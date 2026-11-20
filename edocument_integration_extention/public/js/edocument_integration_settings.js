// Copyright (c) 2026, Dharshan and contributors
// For license information, please see license.txt

const EINVOICE_BE_PROVIDER = "e-invoice.be";
const EINVOICE_BE_DEFAULT_URL = "https://api.e-invoice.be";

frappe.ui.form.on("EDocument Integration Settings", {
	edocument_integrator(frm) {
		if (frm.doc.edocument_integrator === EINVOICE_BE_PROVIDER && !frm.doc.base_url) {
			frm.set_value("base_url", EINVOICE_BE_DEFAULT_URL);
		}
	},

	refresh(frm) {
		if (frm.is_new() || frm.doc.edocument_integrator !== EINVOICE_BE_PROVIDER) {
			return;
		}

		frm.add_custom_button(
			__("Test Connection"),
			() => test_einvoice_be_connection(frm),
			__("Actions")
		);

		frm.add_custom_button(
			__("Verify Peppol ID"),
			() => prompt_and_verify_peppol_id(frm),
			__("Actions")
		);
	},
});

function test_einvoice_be_connection(frm) {
	frm.call({
		method: "test_einvoice_be_connection",
		doc: frm.doc,
		freeze: true,
		freeze_message: __("Testing e-invoice.be connection..."),
		callback(r) {
			const res = r.message || {};
			if (res.status === "success") {
				const tenant = res.tenant || {};
				const peppol_ids = (tenant.peppol_ids || []).join(", ");
				const detail = peppol_ids
					? `${res.message} (${peppol_ids})`
					: res.message;
				frappe.show_alert({ message: detail, indicator: "green" });
			} else {
				frappe.show_alert({
					message: res.message || __("Connection check failed"),
					indicator: "red",
				});
			}
		},
	});
}

function prompt_and_verify_peppol_id(frm) {
	frappe.prompt(
		[
			{
				fieldname: "peppol_id",
				fieldtype: "Data",
				label: __("Peppol ID"),
				description: __("Format: <scheme>:<id>, e.g. 0208:0848934496"),
				reqd: 1,
			},
		],
		(values) => {
			frappe.call({
				method: "edocument_integration_extention.api.verify_einvoice_be_recipient",
				args: {
					peppol_id: values.peppol_id,
					settings_name: frm.doc.name,
				},
				freeze: true,
				freeze_message: __("Checking Peppol registry..."),
				callback(r) {
					const res = r.message || {};
					const card_name = (res.business_card && res.business_card.name) || "-";
					const lines = [
						`<b>${__("Peppol ID")}:</b> ${frappe.utils.escape_html(res.peppol_id || values.peppol_id)}`,
						`<b>${__("Registered")}:</b> ${res.is_valid ? __("Yes") : __("No")}`,
						`<b>${__("Can Receive Invoices")}:</b> ${res.can_receive ? __("Yes") : __("No")}`,
						`<b>${__("Business Name")}:</b> ${frappe.utils.escape_html(card_name)}`,
					];
					frappe.msgprint({
						title: __("Peppol Participant Lookup"),
						message: lines.join("<br>"),
						indicator: res.can_receive ? "green" : res.is_valid ? "orange" : "red",
					});
				},
			});
		},
		__("Verify Peppol ID"),
		__("Verify")
	);
}
