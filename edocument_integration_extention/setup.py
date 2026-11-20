# Copyright (c) 2026, Dharshan and contributors
# For license information, please see license.txt

"""Install, migrate, and uninstall hooks for e-invoice.be integration."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from edocument_integration_extention.einvoice_be_api import PROVIDER_NAME

SETTINGS_DOCTYPE = "EDocument Integration Settings"
INTEGRATOR_FIELD = "edocument_integrator"


def after_install():
	ensure_integrator_option()
	create_custom_fields(get_custom_fields())


def after_migrate():
	ensure_integrator_option()
	create_custom_fields(get_custom_fields())


def before_uninstall():
	if frappe.db.exists(SETTINGS_DOCTYPE, {INTEGRATOR_FIELD: PROVIDER_NAME}):
		return

	ps_name = _get_integrator_property_setter_name()
	if not ps_name:
		return

	current_value = frappe.db.get_value("Property Setter", ps_name, "value") or ""
	options = [opt for opt in _split_options(current_value) if opt != PROVIDER_NAME]
	base_options = _split_options(_get_base_integrator_options())

	if options == base_options:
		frappe.delete_doc("Property Setter", ps_name, ignore_permissions=True)
	else:
		ps_doc = frappe.get_doc("Property Setter", ps_name)
		ps_doc.value = "\n".join(options)
		ps_doc.save(ignore_permissions=True)

	frappe.clear_cache(doctype=SETTINGS_DOCTYPE)


def ensure_integrator_option():
	base_options = _split_options(_get_base_integrator_options())
	ps_name = _get_integrator_property_setter_name()
	existing_ps_value = frappe.db.get_value("Property Setter", ps_name, "value") if ps_name else ""
	existing_options = _split_options(existing_ps_value or "")

	merged: list[str] = []
	for option in [*base_options, *existing_options, PROVIDER_NAME]:
		if option and option not in merged:
			merged.append(option)

	merged_value = "\n".join(merged)
	if ps_name:
		if existing_ps_value != merged_value:
			ps_doc = frappe.get_doc("Property Setter", ps_name)
			ps_doc.value = merged_value
			ps_doc.save(ignore_permissions=True)
	else:
		frappe.make_property_setter(
			{
				"doctype": SETTINGS_DOCTYPE,
				"doctype_or_field": "DocField",
				"fieldname": INTEGRATOR_FIELD,
				"property": "options",
				"value": merged_value,
				"property_type": "Text",
			},
			validate_fields_for_doctype=False,
		)

	frappe.clear_cache(doctype=SETTINGS_DOCTYPE)


def get_custom_fields():
	return {
		SETTINGS_DOCTYPE: [
			{
				"fieldname": "webhook_secret",
				"label": "Webhook Secret",
				"fieldtype": "Password",
				"insert_after": "api_secret",
				"depends_on": "eval:doc.edocument_integrator == 'e-invoice.be'",
				"description": "Signing secret from e-invoice.be used to verify the X-Signature header on incoming webhooks.",
			}
		]
	}


def _get_base_integrator_options() -> str:
	return (
		frappe.db.get_value(
			"DocField",
			{"parent": SETTINGS_DOCTYPE, "fieldname": INTEGRATOR_FIELD},
			"options",
		)
		or ""
	)


def _get_integrator_property_setter_name() -> str | None:
	return frappe.db.get_value(
		"Property Setter",
		{
			"doctype_or_field": "DocField",
			"doc_type": SETTINGS_DOCTYPE,
			"field_name": INTEGRATOR_FIELD,
			"property": "options",
		},
		"name",
	)


def _split_options(raw_options: str) -> list[str]:
	return [line.strip() for line in raw_options.splitlines() if line.strip()]
