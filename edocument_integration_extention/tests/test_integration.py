# Copyright (c) 2026, Dharshan and contributors
# For license information, please see license.txt

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from edocument_integration_extention.api import einvoice_be_webhook, transmit_edocument, webhook
from edocument_integration_extention.einvoice_be_api import (
	DEFAULT_BASE_URL,
	PROVIDER_NAME,
	compute_webhook_signature,
)
from edocument_integration_extention.overrides.edocument_integration_settings import (
	CustomEDocumentIntegrationSettings,
)

PEPPOL_CUSTOMIZATION_ID = "urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0"

PEPPOL_INVOICE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
	xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
	xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
	<cbc:CustomizationID>urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0</cbc:CustomizationID>
	<cbc:ProfileID>urn:fdc:peppol.eu:2017:poacc:billing:01:1.0</cbc:ProfileID>
	<cbc:ID>INV-TEST-001</cbc:ID>
	<cbc:IssueDate>2026-10-01</cbc:IssueDate>
	<cbc:InvoiceTypeCode>380</cbc:InvoiceTypeCode>
	<cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>
	<cac:LegalMonetaryTotal><cbc:PayableAmount currencyID="EUR">0</cbc:PayableAmount></cac:LegalMonetaryTotal>
</Invoice>"""


class TestEInvoiceBeIntegration(IntegrationTestCase):
	def setUp(self):
		# Keep commits inside the IntegrationTestCase transaction rollback.
		self.enterContext(patch.object(frappe.db, "commit"))
		self.peppol_profile = frappe.db.get_value(
			"EDocument Profile",
			{"identifier_value": PEPPOL_CUSTOMIZATION_ID},
			"name",
		)
		if not self.peppol_profile:
			profile = frappe.get_doc(
				{
					"doctype": "EDocument Profile",
					"identifier_namespace": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
					"identifier_element_name": "CustomizationID",
					"identifier_value": PEPPOL_CUSTOMIZATION_ID,
				}
			).insert(ignore_permissions=True)
			self.peppol_profile = profile.name

		self.company = frappe.db.get_value("Company", {}, "name")

	def _post_webhook(self, endpoint, payload, headers=None, content_type="application/json"):
		data = payload if isinstance(payload, str | bytes) else json.dumps(payload)
		request = Request(
			EnvironBuilder(
				method="POST",
				data=data,
				headers=headers or {},
				content_type=content_type,
			).get_environ()
		)
		with patch.object(frappe.local, "request", request, create=True):
			return endpoint()

	def _create_isolated_profile(self, suffix: str) -> str:
		profile = frappe.get_doc(
			{
				"doctype": "EDocument Profile",
				"identifier_namespace": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
				"identifier_element_name": "CustomizationID",
				"identifier_value": f"urn:test:einvoice-be:{suffix}",
			}
		).insert(ignore_permissions=True)
		return profile.name

	def test_meta_and_hooks_registered(self):
		meta = frappe.get_meta("EDocument Integration Settings")
		integrator_field = meta.get_field("edocument_integrator")
		options = [line.strip() for line in (integrator_field.options or "").splitlines() if line.strip()]
		self.assertIn(PROVIDER_NAME, options)

		webhook_secret_field = meta.get_field("webhook_secret")
		self.assertIsNotNone(webhook_secret_field)
		self.assertEqual(webhook_secret_field.fieldtype, "Password")

		doc = frappe.new_doc("EDocument Integration Settings")
		self.assertIsInstance(doc, CustomEDocumentIntegrationSettings)

		self.assertEqual(
			frappe.override_whitelisted_method("edocument_integration.api.transmit_edocument"),
			"edocument_integration_extention.api.transmit_edocument",
		)
		self.assertEqual(
			frappe.override_whitelisted_method("edocument_integration.api.webhook"),
			"edocument_integration_extention.api.webhook",
		)

	def test_settings_validate_defaults_base_url(self):
		settings = frappe.get_doc(
			{
				"doctype": "EDocument Integration Settings",
				"edocument_integrator": PROVIDER_NAME,
				"api_key": "test-api-key",
			}
		).insert(ignore_permissions=True)
		self.assertEqual(settings.base_url, DEFAULT_BASE_URL)

	def test_poll_incoming_documents_imports_and_delegates(self):
		be_settings = frappe.get_doc(
			{
				"doctype": "EDocument Integration Settings",
				"edocument_profile": self.peppol_profile,
				"edocument_integrator": PROVIDER_NAME,
				"api_key": "test-api-key",
			}
		).insert(ignore_permissions=True)

		with patch(
			"edocument_integration_extention.overrides.edocument_integration_settings.poll_inbox",
			return_value={
				"status": "success",
				"invoices": [{"document_id": "doc-be-poll-001", "xml_bytes": PEPPOL_INVOICE_XML}],
			},
		):
			result = be_settings.poll_incoming_documents()

		self.assertEqual(result["status"], "success")
		self.assertEqual(result["processed"], 1)
		edocument_name = result["documents"][0]["edocument"]
		self.assertEqual(frappe.db.get_value("EDocument", edocument_name, "reference"), "doc-be-poll-001")
		self.assertEqual(frappe.db.get_value("EDocument", edocument_name, "direction"), "Incoming")
		attachments = frappe.get_all(
			"File",
			filters={"attached_to_doctype": "EDocument", "attached_to_name": edocument_name},
			fields=["attached_to_field"],
		)
		self.assertEqual(len(attachments), 1)
		self.assertEqual(attachments[0].attached_to_field, "xml_file")

		peppyrus_settings = frappe.get_doc(
			{
				"doctype": "EDocument Integration Settings",
				"edocument_profile": self.peppol_profile,
				"edocument_integrator": "Peppyrus",
				"api_key": "peppyrus-key",
				"api_secret": "unused",
			}
		).insert(ignore_permissions=True)

		with patch(
			"edocument_integration.peppyrus_api.poll_inbox",
			return_value={"status": "success", "invoices": []},
		) as mock_peppyrus_poll:
			delegated = peppyrus_settings.poll_incoming_documents()

		mock_peppyrus_poll.assert_called_once()
		self.assertEqual(delegated["processed"], 0)

	def test_transmit_edocument_einvoice_be_permissions_and_delegation(self):
		tx_profile = self._create_isolated_profile("transmit")
		frappe.get_doc(
			{
				"doctype": "EDocument Integration Settings",
				"edocument_profile": tx_profile,
				"company": self.company,
				"edocument_integrator": PROVIDER_NAME,
				"api_key": "be-api-key",
			}
		).insert(ignore_permissions=True)

		edocument = frappe.get_doc({"doctype": "EDocument", "direction": "Outgoing"}).insert(
			ignore_permissions=True
		)
		frappe.get_doc(
			{
				"doctype": "File",
				"file_name": f"{edocument.name}.xml",
				"attached_to_doctype": "EDocument",
				"attached_to_name": edocument.name,
				"content": PEPPOL_INVOICE_XML,
				"is_private": 1,
			}
		).insert(ignore_permissions=True)
		frappe.db.set_value(
			"EDocument",
			edocument.name,
			{
				"edocument_profile": tx_profile,
				"company": self.company,
				"status": "Validation Successful",
				"direction": "Outgoing",
			},
		)

		with self.set_user("Guest"):
			self.assertRaises(frappe.PermissionError, transmit_edocument, edocument.name)

		with patch(
			"edocument_integration_extention.api.transmit_invoice",
			return_value={
				"status": "TRANSIT",
				"id": "doc-sent-001",
				"document_id": "doc-sent-001",
				"tracking_id": "doc-sent-001",
				"recipient": "0208:0848934496",
			},
		) as mock_transmit:
			result = transmit_edocument(edocument.name)

		mock_transmit.assert_called_once()
		self.assertEqual(result["document_id"], "doc-sent-001")
		edocument.reload()
		self.assertEqual(edocument.status, "Transmission Successful")
		self.assertEqual(edocument.reference, "doc-sent-001")
		self.assertTrue(
			frappe.db.exists(
				"Comment",
				{
					"reference_doctype": "EDocument",
					"reference_name": edocument.name,
					"content": ["like", "%doc-sent-001%"],
				},
			)
		)

		other_profile = self._create_isolated_profile("recommand")
		frappe.get_doc(
			{
				"doctype": "EDocument Integration Settings",
				"edocument_profile": other_profile,
				"company": self.company,
				"edocument_integrator": "Recommand",
				"api_key": "rec-key",
				"api_secret": "rec-secret",
			}
		).insert(ignore_permissions=True)
		other_edoc = frappe.get_doc({"doctype": "EDocument", "direction": "Outgoing"}).insert(
			ignore_permissions=True
		)
		frappe.db.set_value(
			"EDocument",
			other_edoc.name,
			{"edocument_profile": other_profile, "company": self.company},
		)

		with patch(
			"edocument_integration_extention.api.base_api.transmit_edocument",
			return_value={"status": "delegated"},
		) as mock_base_transmit:
			delegated = transmit_edocument(other_edoc.name)

		mock_base_transmit.assert_called_once_with(other_edoc.name)
		self.assertEqual(delegated["status"], "delegated")

	def test_webhook_signature_rejection_and_guest_events(self):
		webhook_secret = "whsec_test_einvoice_be_123456"
		tenant_id = "ten-test-be-01"
		frappe.get_doc(
			{
				"doctype": "EDocument Integration Settings",
				"edocument_profile": self.peppol_profile,
				"edocument_integrator": PROVIDER_NAME,
				"api_key": "be-api-key",
				"account_id": tenant_id,
				"webhook_secret": webhook_secret,
			}
		).insert(ignore_permissions=True)

		received_payload = {
			"id": "evt-recv-1",
			"tenant_id": tenant_id,
			"created_at": 1762780249,
			"type": "document.received",
			"data": {"document_id": "doc-be-recv-001"},
		}
		valid_sig = compute_webhook_signature(received_payload, webhook_secret)

		with self.set_user("Guest"):
			_, missing_status = self._post_webhook(einvoice_be_webhook, received_payload)
			self.assertEqual(missing_status, 401)

			_, bad_status = self._post_webhook(
				einvoice_be_webhook,
				received_payload,
				headers={"X-Signature": "sha256=deadbeef"},
			)
			self.assertEqual(bad_status, 401)

		fake_client = MagicMock()
		fake_client.download_document_ubl.return_value = PEPPOL_INVOICE_XML

		with (
			patch(
				"edocument_integration_extention.webhook.get_einvoice_be_client",
				return_value=fake_client,
			),
			self.set_user("Guest"),
		):
			resp1, status1 = self._post_webhook(
				webhook,
				received_payload,
				headers={"X-Signature": valid_sig},
			)
			resp2, status2 = self._post_webhook(
				webhook,
				received_payload,
				headers={"X-Signature": valid_sig},
			)

		self.assertEqual(status1, 200)
		self.assertTrue(frappe.db.exists("EDocument", {"reference": "doc-be-recv-001"}))
		self.assertEqual(status2, 200)
		self.assertTrue(resp2["result"]["skipped"])
		self.assertEqual(fake_client.download_document_ubl.call_count, 1)

		sent_doc = frappe.get_doc(
			{"doctype": "EDocument", "direction": "Outgoing", "reference": "doc-be-sent-1"}
		).insert(ignore_permissions=True)
		sent_payload = {
			"id": "evt-sent-1",
			"tenant_id": tenant_id,
			"created_at": 1762780250,
			"type": "document.sent",
			"data": {"document_id": "doc-be-sent-1"},
		}
		with self.set_user("Guest"):
			_, sent_status = self._post_webhook(
				einvoice_be_webhook,
				sent_payload,
				headers={"X-Signature": compute_webhook_signature(sent_payload, webhook_secret)},
			)
		self.assertEqual(sent_status, 200)
		self.assertTrue(
			frappe.db.exists(
				"Comment",
				{
					"reference_doctype": "EDocument",
					"reference_name": sent_doc.name,
					"content": ["like", "%doc-be-sent-1%"],
				},
			)
		)

		fail_doc = frappe.get_doc(
			{
				"doctype": "EDocument",
				"direction": "Outgoing",
				"reference": "doc-be-fail-1",
				"status": "Transmission Successful",
			}
		).insert(ignore_permissions=True)
		fail_payload = {
			"id": "evt-fail-1",
			"tenant_id": tenant_id,
			"created_at": 1762780251,
			"type": "document.sent.failed",
			"data": {"document_id": "doc-be-fail-1"},
		}
		with self.set_user("Guest"):
			_, fail_status = self._post_webhook(
				einvoice_be_webhook,
				fail_payload,
				headers={"X-Signature": compute_webhook_signature(fail_payload, webhook_secret)},
			)
		self.assertEqual(fail_status, 200)
		fail_doc.reload()
		self.assertEqual(fail_doc.status, "Transmission Failed")
		self.assertIn("doc-be-fail-1", fail_doc.error)

		recommand_payload = {"eventType": "document.sent", "documentId": "doc-rec-1", "teamId": "team-1"}
		with patch(
			"edocument_integration_extention.api.base_api.webhook",
			return_value=({"status": "success"}, 200),
		) as mock_base_webhook:
			_, rec_status = self._post_webhook(webhook, recommand_payload)

		mock_base_webhook.assert_called_once()
		self.assertEqual(rec_status, 200)
