# Copyright (c) 2026, Dharshan and contributors
# For license information, please see license.txt

"""Whitelisted API overrides and e-invoice.be helper endpoints."""

import json

import frappe
from frappe import _

from edocument_integration import api as base_api
from edocument_integration_extention.einvoice_be_api import (
	PROVIDER_NAME,
	get_einvoice_be_client,
	integration_settings_from_doc,
	transmit_invoice,
	verify_recipient,
)
from edocument_integration_extention.webhook import handle_einvoice_be_webhook


def _get_edocument_integration_settings(profile: str, company: str | None = None) -> dict | None:
	filters = {"edocument_profile": profile}
	if company:
		filters["company"] = company
	names = frappe.get_all("EDocument Integration Settings", filters=filters, limit=1, pluck="name")
	if not names:
		return None
	return integration_settings_from_doc(frappe.get_doc("EDocument Integration Settings", names[0]))


@frappe.whitelist()
def transmit_edocument(edocument_name: str):
	"""Transmit an EDocument via e-invoice.be or delegate to edocument_integration."""
	edocument_doc = frappe.get_doc("EDocument", edocument_name)
	edocument_doc.check_permission("write")

	integration_settings = None
	if edocument_doc.edocument_profile:
		integration_settings = _get_edocument_integration_settings(
			edocument_doc.edocument_profile, edocument_doc.company
		)

	if not integration_settings or integration_settings.get("edocument_integrator") != PROVIDER_NAME:
		return base_api.transmit_edocument(edocument_name)

	try:
		base_api._validate_edocument_for_transmission(edocument_doc)
		xml_bytes = edocument_doc._get_xml_from_attached_files()
		xml_content = xml_bytes.decode("utf-8") if isinstance(xml_bytes, bytes) else xml_bytes

		transmission_result = transmit_invoice(
			xml_content,
			invoice_doc=edocument_doc,
			integration_settings=integration_settings,
		)

		transmission_id = transmission_result.get("id") or transmission_result.get("document_id")
		tracking_id = transmission_result.get("tracking_id", transmission_id)
		status = transmission_result.get("status", "TRANSIT")
		parts = [
			"E-document Transmission Successful:",
			f"• Transmission ID: {transmission_id}",
			f"• Tracking ID: {tracking_id}",
			f"• Status: {status}",
		]
		if transmission_result.get("recipient"):
			parts.append(f"• Recipient: {transmission_result.get('recipient')}")
		edocument_doc.add_comment(comment_type="Info", text="\n".join(parts))

		edocument_doc.reload()
		edocument_doc.status = "Transmission Successful"
		edocument_doc.error = None
		edocument_doc.reference = transmission_id
		edocument_doc.save()

		return transmission_result
	except frappe.ValidationError:
		raise
	except Exception as exc:
		edocument_doc.reload()
		edocument_doc.status = "Transmission Failed"
		edocument_doc.error = str(exc)
		edocument_doc.save()

		frappe.log_error(
			title="E-document Transmission Error",
			message=f"E-document transmission failed for document {edocument_name}: {exc!s}",
		)
		frappe.throw(_("Transmission failed: {0}").format(str(exc)))


# nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method
@frappe.whitelist(allow_guest=True)
def webhook(**kwargs):
	"""Route incoming webhook notifications to e-invoice.be or Recommand."""
	if _is_einvoice_be_webhook_request():
		return handle_einvoice_be_webhook(**kwargs)
	return base_api.webhook(**kwargs)


# nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method
@frappe.whitelist(allow_guest=True)
def einvoice_be_webhook(**kwargs):
	"""Dedicated webhook endpoint for e-invoice.be event notifications."""
	return handle_einvoice_be_webhook(**kwargs)


@frappe.whitelist()
def verify_einvoice_be_recipient(peppol_id: str, settings_name: str | None = None):
	"""Verify whether a Peppol ID is registered and accepts invoices."""
	frappe.has_permission("EDocument Integration Settings", "read", throw=True)
	if not peppol_id or not peppol_id.strip():
		frappe.throw(_("Peppol ID is required."))

	integration_settings = {}
	if settings_name:
		settings_doc = frappe.get_doc("EDocument Integration Settings", settings_name)
		settings_doc.check_permission("read")
		integration_settings = {
			"api_key": settings_doc.api_key,
			"base_url": settings_doc.base_url,
		}

	return verify_recipient(peppol_id.strip(), integration_settings=integration_settings)


@frappe.whitelist()
def validate_edocument_with_einvoice_be(edocument_name: str):
	"""Validate an EDocument XML file against the e-invoice.be validation endpoint."""
	edocument_doc = frappe.get_doc("EDocument", edocument_name)
	edocument_doc.check_permission("read")

	if not edocument_doc.edocument_profile:
		frappe.throw(_("No e-document profile configured for this document."))

	integration_settings = _get_edocument_integration_settings(
		edocument_doc.edocument_profile, edocument_doc.company
	)
	if not integration_settings or integration_settings.get("edocument_integrator") != PROVIDER_NAME:
		frappe.throw(_("No e-invoice.be integration settings found for this document."))

	xml_bytes = edocument_doc._get_xml_from_attached_files()
	client = get_einvoice_be_client(integration_settings)
	return client.validate_ubl(xml_bytes, filename=f"{edocument_doc.name}.xml")


def _is_einvoice_be_webhook_request() -> bool:
	request = getattr(frappe, "request", None)
	if not request:
		return False

	raw_data = request.get_data()
	payload = None
	if raw_data:
		try:
			text = raw_data.decode("utf-8") if isinstance(raw_data, bytes | bytearray) else raw_data
			payload = json.loads(text)
		except (UnicodeDecodeError, json.JSONDecodeError):
			payload = None

	if isinstance(payload, dict) and payload.get("eventType"):
		return False

	if request.headers.get("X-Signature"):
		return True

	return isinstance(payload, dict) and bool(payload.get("type")) and (
		"tenant_id" in payload or "data" in payload
	)
