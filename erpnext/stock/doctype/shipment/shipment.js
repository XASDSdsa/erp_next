// Copyright (c) 2020, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

// Carrier apps register presentation capabilities without replacing native form events.
erpnext.shipment = erpnext.shipment || {};
erpnext.shipment.carriers = erpnext.shipment.carriers || {};
erpnext.shipment.register_carrier = function (name, carrier) {
	erpnext.shipment.carriers[name] = carrier;
};
erpnext.shipment.get_carrier = function (doc) {
	return Object.values(erpnext.shipment.carriers).find((carrier) => carrier.matches(doc));
};
erpnext.shipment.register_carrier("manual", {
	matches: (doc) => doc.service_provider === "其他物流（手工登记）",
	owns_api_ui: true,
	hidden_fields: ["shipment_id", "shipment_amount", "carrier", "carrier_service", "awb_number", "tracking_url", "tracking_status", "tracking_status_info"],
	lock_service_provider: (frm) => !frm.is_new(),
});

frappe.ui.form.on("Shipment", {
	address_query: function (frm, link_doctype, link_name, is_your_company_address) {
		return {
			query: "frappe.contacts.doctype.address.address.address_query",
			filters: {
				link_doctype: link_doctype,
				link_name: link_name,
				is_your_company_address: is_your_company_address,
			},
		};
	},
	contact_query: function (frm, link_doctype, link_name) {
		return {
			query: "frappe.contacts.doctype.contact.contact.contact_query",
			filters: {
				link_doctype: link_doctype,
				link_name: link_name,
			},
		};
	},
	onload: async function (frm) {
		frm.events.manual_shipping_address_queries(frm);
		frm.set_query("pickup_address_name", () => {
			let pickup_from = `pickup_${frappe.model.scrub(frm.doc.pickup_from_type)}`;
			return frm.events.address_query(
				frm,
				frm.doc.pickup_from_type,
				frm.doc[pickup_from],
				frm.doc.pickup_from_type === "Company" ? 1 : 0
			);
		});
		frm.set_query("pickup_contact_name", () => {
			let pickup_from = `pickup_${frappe.model.scrub(frm.doc.pickup_from_type)}`;
			return frm.events.contact_query(frm, frm.doc.pickup_from_type, frm.doc[pickup_from]);
		});
		frm.set_query("delivery_note", "shipment_delivery_note", function () {
			let customer = "";
			if (frm.doc.delivery_to_type == "Customer") {
				customer = frm.doc.delivery_customer;
			}
			if (frm.doc.delivery_to_type == "Company") {
				customer = frm.doc.delivery_company;
			}
			if (customer) {
				return {
					filters: {
						customer: customer,
						docstatus: 1,
						status: ["not in", ["Cancelled"]],
					},
				};
			}
		});
		await frm.events.set_new_shipment_defaults(frm);
	},
	refresh: function (frm) {
		$("div[data-fieldname=pickup_address] > div > .clearfix").hide();
		$("div[data-fieldname=pickup_contact] > div > .clearfix").hide();
		$("div[data-fieldname=delivery_address] > div > .clearfix").hide();
		$("div[data-fieldname=delivery_contact] > div > .clearfix").hide();
		frm.events.prepare_shipment_form(frm);
		frm.events.render_shipment_contents(frm);
	},
	before_save: function (frm) {
		let delivery_to = `delivery_${frappe.model.scrub(frm.doc.delivery_to_type)}`;
		frm.set_value("delivery_to", frm.doc[delivery_to]);
		let pickup_from = `pickup_${frappe.model.scrub(frm.doc.pickup_from_type)}`;
		frm.set_value("pickup", frm.doc[pickup_from]);
	},
	set_pickup_company_address: function (frm) {
		return frm.events.set_address_name(frm, "Company", frm.doc.pickup_company, "Pickup");
	},
	set_delivery_company_address: function (frm) {
		return frm.events.set_address_name(frm, "Company", frm.doc.delivery_company, "Delivery");
	},
	pickup_from_type: function (frm) {
		if (frm.doc.pickup_from_type == "Company") {
			frm.set_value("pickup_company", frappe.defaults.get_default("company"));
			frm.set_value("pickup_customer", "");
			frm.set_value("pickup_supplier", "");
		} else {
			frm.trigger("clear_pickup_fields");
		}
		if (frm.doc.pickup_from_type == "Customer") {
			frm.set_value("pickup_company", "");
			frm.set_value("pickup_supplier", "");
		}
		if (frm.doc.pickup_from_type == "Supplier") {
			frm.set_value("pickup_customer", "");
			frm.set_value("pickup_company", "");
		}
	},
	delivery_to_type: function (frm) {
		if (frm.doc.delivery_to_type == "Company") {
			frm.set_value("delivery_company", frappe.defaults.get_default("company"));
			frm.set_value("delivery_customer", "");
			frm.set_value("delivery_supplier", "");
		} else {
			frm.trigger("clear_delivery_fields");
		}
		if (frm.doc.delivery_to_type == "Customer") {
			frm.set_value("delivery_company", "");
			frm.set_value("delivery_supplier", "");
		}
		if (frm.doc.delivery_to_type == "Supplier") {
			frm.set_value("delivery_customer", "");
			frm.set_value("delivery_company", "");
			frm.toggle_display("shipment_delivery_note", false);
		} else {
			frm.toggle_display("shipment_delivery_note", true);
		}
	},
	delivery_address_name: function (frm) {
		if (frm.doc.delivery_to_type == "Company") {
			erpnext.utils.get_address_display(frm, "delivery_address_name", "delivery_address", true);
		} else {
			erpnext.utils.get_address_display(frm, "delivery_address_name", "delivery_address", false);
		}
	},
	pickup_address_name: function (frm) {
		if (frm.doc.pickup_from_type == "Company") {
			erpnext.utils.get_address_display(frm, "pickup_address_name", "pickup_address", true);
		} else {
			erpnext.utils.get_address_display(frm, "pickup_address_name", "pickup_address", false);
		}
	},
	get_contact_display: function (frm, contact_name, contact_type) {
		frappe.call({
			method: "frappe.contacts.doctype.contact.contact.get_contact_details",
			args: { contact: contact_name },
			callback: function (r) {
				if (r.message) {
					if (!(r.message.contact_email || r.message.contact_phone || r.message.contact_mobile)) {
						if (contact_type == "Delivery") {
							frm.set_value("delivery_contact_name", "");
							frm.set_value("delivery_contact", "");
						} else {
							frm.set_value("pickup_contact_name", "");
							frm.set_value("pickup_contact", "");
						}
						frappe.throw(
							__("Email or Phone/Mobile of the Contact are mandatory to continue.") +
								"</br>" +
								__("Please set Email/Phone for the contact") +
								` <a href="${frappe.utils.get_form_link(
									"Contact",
									contact_name
								)}">${frappe.utils.escape_html(contact_name)}</a>`
						);
					}
					let contact_display = r.message.contact_display;
					if (r.message.contact_email) {
						contact_display += "<br>" + r.message.contact_email;
					}
					if (r.message.contact_phone) {
						contact_display += "<br>" + r.message.contact_phone;
					}
					if (r.message.contact_mobile && !r.message.contact_phone) {
						contact_display += "<br>" + r.message.contact_mobile;
					}
					if (contact_type == "Delivery") {
						frm.set_value("delivery_contact", contact_display);
						if (r.message.contact_email) {
							frm.set_value("delivery_contact_email", r.message.contact_email);
						}
					} else {
						frm.set_value("pickup_contact", contact_display);
						if (r.message.contact_email) {
							frm.set_value("pickup_contact_email", r.message.contact_email);
						}
					}
				}
			},
		});
	},
	delivery_contact_name: function (frm) {
		if (frm.doc.delivery_contact_name) {
			frm.events.get_contact_display(frm, frm.doc.delivery_contact_name, "Delivery");
		}
	},
	pickup_contact_name: function (frm) {
		if (frm.doc.pickup_contact_name) {
			frm.events.get_contact_display(frm, frm.doc.pickup_contact_name, "Pickup");
		}
	},
	pickup_contact_person: function (frm) {
		if (frm.doc.pickup_from_type === "Company") {
			return frm.events.set_company_contact(frm, "Pickup");
		}
	},
	set_company_contact: function (frm, delivery_type) {
		// Contact defaults come from the selected user. Carrier validation owns any
		// additional booking requirements; missing optional profile data must not clear the company.
		const pickup = delivery_type !== "Delivery";
		const user = pickup ? frm.doc.pickup_contact_person || frappe.session.user : frappe.session.user;
		if (pickup && !frm.doc.pickup_contact_person) {
			// This field's native event performs the lookup once.
			return frm.set_value("pickup_contact_person", user);
		}
		const doc = frm.doc;
		return frappe.db.get_value(
			"User",
			{ name: user },
			["full_name", "first_name", "last_name", "email", "phone", "mobile_no"],
			(r) => {
				const prefix = pickup ? "pickup" : "delivery";
				if (!r || frm.doc !== doc || (pickup && frm.doc.pickup_contact_person !== user)) return;
				const full_name = r.full_name || [r.first_name, r.last_name].filter(Boolean).join(" ") || user;
				const contact_display = [full_name, r.email, r.phone || r.mobile_no]
					.filter(Boolean).map(frappe.utils.escape_html).join("<br>");
				return frm.set_value({
					[`${prefix}_contact`]: contact_display,
					[`${prefix}_contact_email`]: r.email || "",
				});
			}
		);
	},
	pickup_company: async function (frm) {
		if (frm.doc.pickup_from_type == "Company" && frm.doc.pickup_company) {
			await frm.trigger("set_pickup_company_address");
			await frm.events.set_company_contact(frm, "Pickup");
		}
	},
	delivery_company: function (frm) {
		if (frm.doc.delivery_to_type == "Company" && frm.doc.delivery_company) {
			frm.trigger("set_delivery_company_address");
			frm.events.set_company_contact(frm, "Delivery");
		}
	},
	delivery_customer: function (frm) {
		frm.trigger("clear_delivery_fields");
		if (frm.doc.delivery_customer) {
			frm.events.set_address_name(frm, "Customer", frm.doc.delivery_customer, "Delivery");
			frm.events.set_contact_name(frm, "Customer", frm.doc.delivery_customer, "Delivery");
		}
	},
	delivery_supplier: function (frm) {
		frm.trigger("clear_delivery_fields");
		if (frm.doc.delivery_supplier) {
			frm.events.set_address_name(frm, "Supplier", frm.doc.delivery_supplier, "Delivery");
			frm.events.set_contact_name(frm, "Supplier", frm.doc.delivery_supplier, "Delivery");
		}
	},
	pickup_customer: function (frm) {
		if (frm.doc.pickup_customer) {
			frm.events.set_address_name(frm, "Customer", frm.doc.pickup_customer, "Pickup");
			frm.events.set_contact_name(frm, "Customer", frm.doc.pickup_customer, "Pickup");
		}
	},
	pickup_supplier: function (frm) {
		if (frm.doc.pickup_supplier) {
			frm.events.set_address_name(frm, "Supplier", frm.doc.pickup_supplier, "Pickup");
			frm.events.set_contact_name(frm, "Supplier", frm.doc.pickup_supplier, "Pickup");
		}
	},
	set_address_name: function (frm, ref_doctype, ref_docname, delivery_type) {
		const doc = frm.doc;
		const prefix = delivery_type === "Delivery" ? "delivery" : "pickup";
		const fieldname = `${prefix}_address_name`;
		const previous_address = doc[fieldname];
		return frappe.call({
			method: "erpnext.stock.doctype.shipment.shipment.get_address_name",
			args: {
				ref_doctype: ref_doctype,
				docname: ref_docname,
			},
			callback: function (r) {
				if (r.message && frm.doc === doc && doc[fieldname] === previous_address
					&& doc[`${prefix}_${frappe.model.scrub(ref_doctype)}`] === ref_docname) {
					return frm.set_value(fieldname, r.message);
				}
			},
		});
	},
	set_contact_name: function (frm, ref_doctype, ref_docname, delivery_type) {
		frappe.call({
			method: "erpnext.stock.doctype.shipment.shipment.get_contact_name",
			args: {
				ref_doctype: ref_doctype,
				docname: ref_docname,
			},
			callback: function (r) {
				if (r.message) {
					if (delivery_type == "Delivery") {
						frm.set_value("delivery_contact_name", r.message);
					} else {
						frm.set_value("pickup_contact_name", r.message);
					}
				}
			},
		});
	},
	add_template: function (frm) {
		if (frm.doc.parcel_template) {
			frappe.model.with_doc("Shipment Parcel Template", frm.doc.parcel_template, () => {
				let parcel_template = frappe.model.get_doc(
					"Shipment Parcel Template",
					frm.doc.parcel_template
				);
				let row = frappe.model.add_child(frm.doc, "Shipment Parcel", "shipment_parcel");
				row.length = parcel_template.length;
				row.width = parcel_template.width;
				row.height = parcel_template.height;
				row.weight = parcel_template.weight;
				frm.refresh_fields("shipment_parcel");
			});
		}
	},
	pickup_date: function (frm) {
		if (frm.doc.pickup_date < frappe.datetime.get_today()) {
			frappe.throw(__("Pickup Date cannot be before this day"));
		}
	},
	clear_pickup_fields: function (frm) {
		let fields = [
			"pickup_address_name",
			"pickup_contact_name",
			"pickup_address",
			"pickup_contact",
			"pickup_contact_email",
			"pickup_contact_person",
		];
		for (let field of fields) {
			frm.set_value(field, "");
		}
	},
	clear_delivery_fields: function (frm) {
		let fields = [
			"delivery_address_name",
			"delivery_contact_name",
			"delivery_address",
			"delivery_contact",
			"delivery_contact_email",
		];
		for (let field of fields) {
			frm.set_value(field, "");
		}
	},
	// Read-only contents come from the native document load, never from carrier form state.
	render_shipment_contents(frm) {
		const $host = frm.fields_dict.shipment_contents?.$wrapper;
		if (!$host) return;
		const escape = (value) => frappe.utils.escape_html(String(value ?? ""));
		const restricted = frm.doc.__onload?.shipment_contents_restricted;
		if (restricted) {
			$host.html(`<p class="text-muted">${escape(restricted.message || __("You do not have permission to view the linked contents."))}</p>`);
			return;
		}
		const contents = frm.doc.__onload?.shipment_contents;
		if (!Array.isArray(contents)) {
			$host.html(`<p class="text-muted">${escape(__("Save or reload the Shipment to view its linked contents."))}</p>`);
			return;
		}
		const linked_notes = (frm.doc.shipment_delivery_note || []).map((row) => row.delivery_note).filter(Boolean);
		const visible = contents.filter((group) => linked_notes.includes(group.delivery_note));
		const missing = linked_notes.filter((name) => !visible.some((group) => group.delivery_note === name));
		if (!linked_notes.length) {
			$host.html(`<p class="text-muted">${escape(__("No goods linked. Associate a Delivery Note first."))}</p>`);
			return;
		}
		const headers = ["Item Code", "Item Name", "Quantity", "UOM", "Rate", "Amount", "Warehouse"];
		const html = visible.map((group) => {
			const rows = (group.items || []).map((item) => {
				const money = (value) => value == null ? "" : format_currency(value, group.currency);
				const values = [item.item_code, item.item_name, item.qty, item.uom,
					money(item.rate), money(item.amount), item.warehouse || group.warehouse];
				return `<tr>${values.map((value, index) => `<td${[2, 4, 5].includes(index) ? ' class="text-right"' : ""}>${escape(value)}</td>`).join("")}</tr>`;
			}).join("");
			return `<section class="shipment-contents-group">
				<p><strong>${escape(__("Delivery Note"))}: ${escape(group.delivery_note)}</strong>
				${group.warehouse ? ` · ${escape(__("Source Warehouse"))}: ${escape(group.warehouse)}` : ""}</p>
				<div class="table-responsive"><table class="table table-bordered table-sm">
					<thead><tr>${headers.map((label) => `<th scope="col">${escape(__(label))}</th>`).join("")}</tr></thead>
					<tbody>${rows}</tbody>
				</table></div>
			</section>`;
		}).join("");
		$host.html(html);
		if (missing.length) {
			$host.append(`<p class="text-muted">${escape(__("Save or reload the Shipment to view its linked contents."))}</p>`);
		}
		frm.events.render_shipment_sticker_review(frm, $host, visible);
	},

	shipment_sticker_cards(contents) {
		const cards = new Map();
		const add = (sticker, products) => {
			if (!sticker.item_code) return;
			if (!cards.has(sticker.item_code)) cards.set(sticker.item_code, { ...sticker, products: new Set() });
			for (const product of products.filter(Boolean)) cards.get(sticker.item_code).products.add(product);
		};
		for (const group of contents || []) {
			for (const item of group.items || []) {
				for (const sticker of item.stickers || []) add(sticker, [item.item_name || item.item_code]);
			}
			for (const sticker of group.stickers || []) add(sticker, sticker.products || []);
		}
		return [...cards.values()].map((card) => ({ ...card, products: [...card.products] }));
	},

	shipment_sticker_image_url(value) {
		if (typeof value !== "string" || !/^\/(?:private\/)?files\//.test(value)) return "";
		try {
			const decoded = decodeURIComponent(value);
			if (/[\\<>?#\x00-\x1f]/.test(decoded) || decoded.split("/").some((part) => part === "." || part === "..")) return "";
			return value;
		} catch (_) {
			return "";
		}
	},

	shipment_sticker_review_html(frm, cards) {
		if (!cards.length) return "";
		const escape = (value) => frappe.utils.escape_html(String(value || ""));
		const html = cards.map((card, index) => {
			const original = frm.events.shipment_sticker_image_url(card.image_url);
			const thumbnail = frm.events.shipment_sticker_image_url(card.thumbnail_url) || original;
			const ready = card.image_status === "ready" && original;
			const state = card.image_status === "restricted" ? "无权查看图片，请联系管理员"
				: card.image_status === "unavailable" ? "图片不可用，请联系业务员核对"
					: "缺少贴纸图片，请联系业务员补齐";
			const picture = ready ? `<button type="button" class="shipment-sticker-thumb" data-sticker-index="${index}" aria-label="${escape("放大查看贴纸：" + (card.item_name || card.item_code))}">
				<img src="${escape(thumbnail)}" alt="${escape(card.item_name || card.item_code)}" width="96" height="96" loading="lazy" decoding="async"><span>点击放大</span></button>`
				: '<div class="shipment-sticker-no-image" aria-hidden="true">暂无预览</div>';
			return `<article class="shipment-sticker-card">${picture}<div class="shipment-sticker-info">
				<strong>${escape(card.item_name || card.item_code)}</strong>
				${card.model ? `<div>型号：${escape(card.model)}</div>` : ""}
				${card.version ? `<div>版本：${escape(card.version)}</div>` : ""}
				<div class="shipment-sticker-products">对应物料：${card.products.map(escape).join("、")}</div>
				<div class="shipment-sticker-image-state ${ready ? "hide" : ""}" role="status">${ready ? "" : escape(state)}</div>
			</div></article>`;
		}).join("");
		return `<section class="shipment-sticker-review" aria-label="贴纸核对">
			<style>
			.shipment-sticker-review { margin-top: 16px; }
			.shipment-sticker-review h5 { margin: 0 0 10px; font-size: var(--text-base, 14px); font-weight: 600; }
			.shipment-sticker-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 300px), 1fr)); gap: 10px; }
			.shipment-sticker-card { display: flex; align-items: flex-start; gap: 12px; padding: 12px; border: 1px solid var(--border-color, #ddd); border-radius: 8px; background: var(--fg-color, #fff); min-width: 0; }
			.shipment-sticker-thumb { flex: 0 0 96px; border: 0; padding: 0; color: var(--text-color, #333); background: transparent; cursor: zoom-in; }
			.shipment-sticker-thumb img { display: block; width: 96px; height: 96px; object-fit: contain; border-radius: 4px; background: var(--control-bg, #f5f5f5); }
			.shipment-sticker-thumb span { display: block; margin-top: 4px; font-size: 12px; color: var(--text-muted, #666); }
			.shipment-sticker-thumb:focus-visible { outline: 2px solid var(--primary, #2490ef); outline-offset: 3px; }
			.shipment-sticker-info { min-width: 0; font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
			.shipment-sticker-info strong { display: block; font-size: 13px; margin-bottom: 4px; }
			.shipment-sticker-products { margin-top: 4px; color: var(--text-muted, #666); }
			.shipment-sticker-image-state { margin-top: 6px; color: var(--orange-700, #9a4d00); }
			.shipment-sticker-no-image { flex: 0 0 96px; min-height: 96px; display: grid; place-items: center; border-radius: 4px; color: var(--text-muted, #666); background: var(--control-bg, #f5f5f5); font-size: 12px; }
			</style><h5>贴纸核对</h5><div class="shipment-sticker-cards">${html}</div></section>`;
	},

	render_shipment_sticker_review(frm, $host, contents) {
		const cards = frm.events.shipment_sticker_cards(contents);
		if (!cards.length) return;
		const $review = $(frm.events.shipment_sticker_review_html(frm, cards)).appendTo($host);
		$review.find(".shipment-sticker-thumb img").on("error", function () {
			$(this).hide();
			$(this).closest(".shipment-sticker-card").find(".shipment-sticker-image-state").text("图片加载失败，请检查网络或联系业务员核对").removeClass("hide");
		});
		$review.find("[data-sticker-index]").on("click", function () {
			const card = cards[Number(this.dataset.stickerIndex)];
			const url = card && frm.events.shipment_sticker_image_url(card.image_url);
			if (!url) return;
			const escape = (value) => frappe.utils.escape_html(String(value || ""));
			const dialog = new frappe.ui.Dialog({ title: "贴纸核对", size: "large", fields: [{ fieldtype: "HTML", fieldname: "picture" }] });
			dialog.fields_dict.picture.$wrapper.html(`<div style="overflow-wrap:anywhere"><strong>${escape(card.item_name || card.item_code)}</strong>
				<div class="text-muted small">${escape([card.model, card.version].filter(Boolean).join(" · "))}</div>
				<p class="small">对应物料：${card.products.map(escape).join("、")}</p>
				<img src="${escape(url)}" alt="${escape(card.item_name || card.item_code)}" style="display:block;max-width:100%;max-height:65vh;object-fit:contain;margin:auto">
				<p class="shipment-sticker-full-error hide text-muted" role="status">原图加载失败，请检查网络或联系业务员核对。</p></div>`);
			dialog.fields_dict.picture.$wrapper.find("img").on("error", function () {
				$(this).hide();
				dialog.fields_dict.picture.$wrapper.find(".shipment-sticker-full-error").removeClass("hide");
			});
			dialog.show();
		});
	},

	// Carrier-neutral form behavior stays with the Shipment DocType.
	set_new_shipment_defaults: async function (frm) {
		if (!frm.is_new()) return;
		if (!frm.doc.pickup_date) {
			await frm.set_value("pickup_date", frappe.datetime.get_today());
		}
		if (frm.doc.pickup_from_type !== "Company") return;
		if (!frm.doc.pickup_company) {
			const company = frappe.defaults.get_user_default("Company") || frappe.defaults.get_global_default("company");
			if (company) {
				// The native company event fills its address and contact.
				await frm.set_value("pickup_company", company);
			}
		} else {
			if (!frm.doc.pickup_address_name) await frm.trigger("set_pickup_company_address");
			if (!frm.doc.pickup_contact) await frm.events.set_company_contact(frm, "Pickup");
		}
	},
	service_provider: function (frm) {
		frm.events.prepare_shipment_form(frm);
	},
	shipping_api_enabled: function (frm) {
		return !erpnext.shipment.get_carrier(frm.doc)?.owns_api_ui;
	},
	prepare_shipment_form: function (frm) {
		const carrier = erpnext.shipment.get_carrier(frm.doc);
		// Restore the native field metadata before applying this carrier's presentation.
		// A provider switch must not retain the previous carrier's field overrides.
		for (const [fieldname, properties] of Object.entries(frm._shipment_carrier_properties || {})) {
			for (const [property, value] of Object.entries(properties)) {
				frm.set_df_property(fieldname, property, value);
			}
		}
		frm._shipment_carrier_properties = {};
		const set_property = (fieldname, property, value) => {
			const field = frm.fields_dict[fieldname];
			if (!field) return;
			(frm._shipment_carrier_properties[fieldname] ||= {})[property] = field.df[property] || 0;
			frm.set_df_property(fieldname, property, value);
		};
		for (const fieldname of carrier?.hidden_fields || []) set_property(fieldname, "hidden", 1);
		if (carrier?.lock_service_provider?.(frm)) set_property("service_provider", "read_only", 1);
		const section = frm.fields_dict.shipment_information_section;
		if (section?.wrapper) {
			const columns = $(section.wrapper).closest(".form-section").find(".form-column");
			const single_column = !!carrier?.single_column_information;
			if (columns.length > 1) {
				columns.eq(1).toggle(!single_column);
				columns.eq(0).toggleClass("col-sm-12", single_column).toggleClass("col-sm-6", !single_column);
			}
		}
		frm.events.manual_shipping_prepare(frm);
		if (!frm.is_new() && cint(frm.doc.docstatus) === 0 && !frm.events.manual_is_shipping(frm)) {
			frm.dashboard.set_headline(
				__("This shipment is still a Draft. Click Submit in the top right when the information is complete.")
			);
		}
	},
	manual_destination(frm) {
		frm.events.manual_shipping_address_queries(frm);
		frm.events.manual_shipping_prepare(frm);
	},

	manual_transport_status(frm) {
		frm.events.manual_shipping_prepare(frm);
	},

	manual_freight_recorded(frm) {
		frm.events.manual_shipping_prepare(frm);
	},

	manual_carrier(frm) {
		frm.events.manual_shipping_check_waybill(frm);
	},

	manual_waybill(frm) {
		frm.events.manual_shipping_check_waybill(frm);
	},

	manual_shipping_check_waybill(frm) {
		if (!frm.events.manual_is_shipping(frm)) return;
		if (cint(frm.doc.manual_waybill_override)) frm.set_value("manual_waybill_override", 0);
		const carrier = frm.doc.manual_carrier;
		const waybill = frm.doc.manual_waybill;
		frm.set_df_property("manual_waybill", "description", "按面单原样填写一个单号，保留前导零；此处不联网验证真伪。");
		if (!carrier || !waybill) return;
		frappe.call({
			method: "erpnext.stock.doctype.shipment.manual_shipping.check_waybill",
			args: { carrier, waybill },
			callback(r) {
				if (r.exc || !r.message || !frm.events.manual_is_shipping(frm)
					|| carrier !== frm.doc.manual_carrier || waybill !== frm.doc.manual_waybill) return;
				const hint = frappe.utils.escape_html(r.message.hint || "");
				frm.set_df_property("manual_waybill", "description", r.message.unusual
					? `<span class="text-warning">单号格式与常见规则不同。${hint}；请核对原面单，勾选“单号格式已核对”并填写说明。</span>`
					: `${hint}（仅格式检查，未联网核验）`);
			},
		});
	},

	manual_is_shipping(frm) {
		return frm.doc.service_provider === "其他物流（手工登记）";
	},

	manual_shipping_address_queries(frm) {
		frm.set_query("delivery_address_name", () => {
			if (frm.events.manual_is_shipping(frm)) return { filters: { disabled: 0 } };
			const field = `delivery_${frappe.model.scrub(frm.doc.delivery_to_type)}`;
			return frm.events.address_query(frm, frm.doc.delivery_to_type, frm.doc[field],
				frm.doc.delivery_to_type === "Company" ? 1 : 0);
		});
		frm.set_query("delivery_contact_name", () => {
			if (frm.events.manual_is_shipping(frm)) return {};
			const field = `delivery_${frappe.model.scrub(frm.doc.delivery_to_type)}`;
			return frm.events.contact_query(frm, frm.doc.delivery_to_type, frm.doc[field]);
		});
	},

	manual_shipping_prepare(frm) {
		const manual = frm.events.manual_is_shipping(frm);
		frm.remove_custom_button("补登记运费");
		frm.events.manual_shipping_address_queries(frm);
		if (!manual) {
			if (frm._manual_address_descriptions) {
				Object.entries(frm._manual_address_descriptions).forEach(([field, description]) => {
					frm.set_df_property(field, "description", description);
				});
				delete frm._manual_address_descriptions;
				frm.dashboard.set_headline("");
			}
			return;
		}
		frm._manual_address_descriptions ||= Object.fromEntries(
			["delivery_address_name", "delivery_contact_name"].map((field) =>
				[field, frm.fields_dict[field]?.df.description || ""])
		);
		frm.set_df_property("delivery_address_name", "description",
			"选择本次实际收货地址，可选择货代或港口仓地址；不会修改客户档案中的默认地址。");
		frm.set_df_property("delivery_contact_name", "description", "选择本次实际收件联系人。");
		if (cint(frm.doc.docstatus) === 1 && frm.doc.manual_transport_status) {
			frm.page.set_indicator(frm.doc.manual_transport_status,
				["已送达", "已交接货代"].includes(frm.doc.manual_transport_status) ? "green" : "blue");
		}
		frm.dashboard.set_headline("其他物流仅登记已有单号、人工运输状态和运费；货代仓签收不代表国外客户签收，登记费用不会自动记账或付款。");
		if (cint(frm.doc.docstatus) === 2 && (frm.perm || []).some((p) => p.write)) {
			frm.add_custom_button("补登记运费", () => frm.events.manual_cancelled_freight(frm));
		}
	},

	manual_cancelled_freight(frm) {
		const dialog = new frappe.ui.Dialog({
			title: "取消运单 · 补登记运费",
			fields: [
				{ fieldtype: "HTML", options: "<p>登记取消后仍产生的实际费用，保留原单号和修改记录；此处不会生成会计凭证或付款。</p>" },
				{ fieldname: "amount", label: "运费金额", fieldtype: "Currency", options: "currency", reqd: 1, default: frm.doc.manual_freight_amount || 0 },
				{ fieldname: "currency", label: "币种", fieldtype: "Link", options: "Currency", reqd: 1, default: frm.doc.manual_freight_currency || "CNY" },
				{ fieldname: "settlement_status", label: "结算状态（人工记录）", fieldtype: "Select", options: "待结算\n已结算（人工确认）", default: frm.doc.manual_settlement_status || "待结算", reqd: 1 },
				{ fieldname: "evidence", label: "凭据", fieldtype: "Attach", default: frm.doc.manual_evidence || "" },
				{ fieldname: "note", label: "本次登记或更正原因", fieldtype: "Small Text", reqd: 1 },
			],
			primary_action_label: "登记运费",
			primary_action(values) {
				frappe.call({
					method: "erpnext.stock.doctype.shipment.manual_shipping.record_cancelled_freight",
					type: "POST", freeze: true, freeze_message: "正在登记运费",
					args: { ...values, shipment: frm.doc.name, expected_modified: frm.doc.modified },
					callback(r) {
						if (r.exc || !r.message) return;
						dialog.hide();
						frappe.show_alert({ message: r.message.message, indicator: "green" });
						frm.reload_doc();
					},
				});
			},
		});
		dialog.show();
	},

	remove_email_row: function (frm, table, fieldname) {
		$.each(frm.doc[table] || [], function (i, detail) {
			if (detail.email === fieldname) {
				cur_frm.get_field(table).grid.grid_rows[i].remove();
			}
		});
	},
});

frappe.ui.form.on("Shipment Delivery Note", {
	delivery_note: function (frm, cdt, cdn) {
		let row = locals[cdt][cdn];
		if (row.delivery_note) {
			let row_index = row.idx - 1;
			if (validate_duplicate(frm, "shipment_delivery_note", row.delivery_note, row_index)) {
				frappe.throw(
					__("You have entered a duplicate Delivery Note on Row") +
						` ${row.idx}. ` +
						__("Please rectify and try again.")
				);
			}
		}
		frm.events.render_shipment_contents(frm);
	},
	shipment_delivery_note_remove: function (frm) {
		frm.events.render_shipment_contents(frm);
	},
	grand_total: function (frm, cdt, cdn) {
		let row = locals[cdt][cdn];
		if (row.grand_total) {
			var value_of_goods = parseFloat(frm.doc.value_of_goods) + parseFloat(row.grand_total);
			frm.set_value("value_of_goods", Math.round(value_of_goods));
			frm.refresh_fields("value_of_goods");
		}
	},
});

var validate_duplicate = function (frm, table, fieldname, index) {
	return table === "shipment_delivery_note"
		? frm.doc[table].some((detail, i) => detail.delivery_note === fieldname && !(index === i))
		: frm.doc[table].some((detail, i) => detail.email === fieldname && !(index === i));
};
