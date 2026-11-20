# Copyright (c) 2026, Dharshan and contributors
# For license information, please see license.txt

"""DocType controller override for EDocument Integration Settings."""

import frappe
from frappe import _

from edocument_integration.edocument_integration.doctype.edocument_integration_settings.edocument_integration_settings import (
	EDocumentIntegrationSettings,
)
from edocument_integration_extention.einvoice_be_api import (
	DEFAULT_BASE_URL,
	PROVIDER_NAME,
	poll_inbox,
	validate_einvoice_be_connection,
)


class CustomEDocumentIntegrationSettings(EDocumentIntegrationSettings):
	"""Extend EDocument Integration Settings with e-invoice.be support."""

	def validate(self):
		if hasattr(super(), "validate"):
			super().validate()
		if self.edocument_integrator == PROVIDER_NAME and not self.base_url:
			self.base_url = DEFAULT_BASE_URL

	@frappe.whitelist()
	def poll_incoming_documents(self):
		if self.edocument_integrator != PROVIDER_NAME:
			return super().poll_incoming_documents()

		if not self.edocument_profile:
			frappe.throw(_("EDocument Profile is required"))

		integration_settings = {
			"api_key": self.api_key,
			"base_url": self.base_url,
			"company": self.company,
			"company_id": self.company_id,
			"account_id": self.account_id,
			"edocument_integrator": self.edocument_integrator,
			"edocument_profile": self.edocument_profile,
		}

		poll_result = poll_inbox(integration_settings=integration_settings, company_id=self.company_id)
		documents = poll_result.get("invoices", [])
		if not documents:
			return {"status": "success", "message": "No new documents found", "processed": 0}

		processed = []
		skipped = []
		for document_data in documents:
			try:
				xml_bytes = document_data.get("xml_bytes")
				document_id = document_data.get("document_id")

				if not xml_bytes:
					frappe.log_error(
						title="Document Processing Error",
						message=f"No xml_bytes found for document {document_id}",
					)
					continue

				if isinstance(xml_bytes, str):
					xml_bytes = xml_bytes.encode("utf-8")
				elif not isinstance(xml_bytes, bytes):
					frappe.log_error(
						title="Document Processing Error",
						message=f"Invalid xml_bytes type: {type(xml_bytes)} for document {document_id}",
					)
					continue

				result = self.process_incoming_document(xml_bytes, document_id)
				if result.get("skipped"):
					skipped.append(result)
				else:
					processed.append(result)
			except Exception as exc:
				frappe.log_error(
					title="Document Processing Error",
					message=(
						f"Failed to process document {document_data.get('document_id')}: {exc!s}\n"
						f"Traceback: {frappe.get_traceback()}"
					),
				)

		message_parts = []
		if processed:
			message_parts.append(f"Processed {len(processed)} document(s)")
		if skipped:
			message_parts.append(f"Skipped {len(skipped)} duplicate(s)")

		return {
			"status": "success",
			"message": ", ".join(message_parts) if message_parts else "No new documents found",
			"processed": len(processed),
			"skipped": len(skipped),
			"documents": processed,
		}

	@frappe.whitelist()
	def test_einvoice_be_connection(self):
		self.check_permission("read")
		if self.edocument_integrator != PROVIDER_NAME:
			frappe.throw(_("EDocument Integrator must be set to e-invoice.be"))
		if not self.api_key:
			frappe.throw(_("API Key is required to test the e-invoice.be connection"))
		return validate_einvoice_be_connection(self.api_key, self.base_url)
