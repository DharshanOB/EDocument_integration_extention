# Copyright (c) 2026, Dharshan and contributors
# For license information, please see license.txt

import json
import types
import unittest
from unittest.mock import Mock, patch

from edocument_integration_extention import einvoice_be_api

BILLING_INVOICE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
	xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
	xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
	<cbc:CustomizationID>urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0</cbc:CustomizationID>
	<cbc:ProfileID>urn:fdc:peppol.eu:2017:poacc:billing:01:1.0</cbc:ProfileID>
	<cbc:ID>INV-2026-001</cbc:ID>
	<cac:AccountingSupplierParty>
		<cac:Party>
			<cbc:EndpointID schemeID="0208">1018265814</cbc:EndpointID>
		</cac:Party>
	</cac:AccountingSupplierParty>
	<cac:AccountingCustomerParty>
		<cac:Party>
			<cbc:EndpointID schemeID="0208">0208:0848934496</cbc:EndpointID>
		</cac:Party>
	</cac:AccountingCustomerParty>
</Invoice>"""

CREDIT_NOTE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<CreditNote xmlns="urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2"
	xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
	xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
	<cbc:CustomizationID>urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0</cbc:CustomizationID>
	<cbc:ID>CN-2026-001</cbc:ID>
	<cac:AccountingSupplierParty>
		<cac:Party>
			<cbc:EndpointID schemeID="0208">1018265814</cbc:EndpointID>
		</cac:Party>
	</cac:AccountingSupplierParty>
	<cac:AccountingCustomerParty>
		<cac:Party>
			<cbc:EndpointID schemeID="9930">DE123456789</cbc:EndpointID>
		</cac:Party>
	</cac:AccountingCustomerParty>
</CreditNote>"""

SELF_BILLING_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
	xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
	xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
	<cbc:CustomizationID>urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:selfbilling:3.0</cbc:CustomizationID>
	<cbc:ID>SB-2026-001</cbc:ID>
	<cac:AccountingSupplierParty>
		<cac:Party>
			<cbc:EndpointID schemeID="0208">0111111111</cbc:EndpointID>
		</cac:Party>
	</cac:AccountingSupplierParty>
	<cac:AccountingCustomerParty>
		<cac:Party>
			<cbc:EndpointID schemeID="0208">0222222222</cbc:EndpointID>
		</cac:Party>
	</cac:AccountingCustomerParty>
</Invoice>"""


class MockHTTPResponse:
	def __init__(self, payload, status_code=200, headers=None, content=None):
		self._payload = payload
		self.status_code = status_code
		self.ok = status_code < 400
		self.headers = headers or {}
		if content is not None:
			self.content = content
			self.text = content.decode("utf-8", errors="replace")
		elif isinstance(payload, str):
			self.text = payload
			self.content = payload.encode("utf-8")
		elif payload is None:
			self.text = ""
			self.content = b""
		else:
			self.text = json.dumps(payload)
			self.content = self.text.encode("utf-8")

	def json(self):
		if isinstance(self._payload, str):
			return json.loads(self._payload)
		return self._payload


class TestEInvoiceBeAPI(unittest.TestCase):
	def setUp(self):
		self.log_patcher = patch("edocument_integration_extention.einvoice_be_api.frappe.log_error")
		self.mock_log_error = self.log_patcher.start()

	def tearDown(self):
		self.log_patcher.stop()

	def test_client_headers_bearer_and_no_default_json_content_type(self):
		client = einvoice_be_api.EInvoiceBeAPIClient("secret-token", "https://api.e-invoice.be/")
		self.assertEqual(client.base_url, "https://api.e-invoice.be")
		self.assertEqual(client.session.headers["Authorization"], "Bearer secret-token")
		self.assertNotIn("Content-Type", client.session.headers)

	def test_extract_peppol_endpoints_billing_credit_note_self_billing_and_missing(self):
		billing = einvoice_be_api._extract_peppol_endpoints(BILLING_INVOICE_XML)
		self.assertEqual(billing["sender_peppol_scheme"], "0208")
		self.assertEqual(billing["sender_peppol_id"], "1018265814")
		self.assertEqual(billing["receiver_peppol_scheme"], "0208")
		self.assertEqual(billing["receiver_peppol_id"], "0848934496")

		credit_note = einvoice_be_api._extract_peppol_endpoints(CREDIT_NOTE_XML)
		self.assertEqual(credit_note["sender"], "0208:1018265814")
		self.assertEqual(credit_note["recipient"], "9930:DE123456789")

		self_billing = einvoice_be_api._extract_peppol_endpoints(SELF_BILLING_XML)
		self.assertEqual(self_billing["sender"], "0208:0222222222")
		self.assertEqual(self_billing["recipient"], "0208:0111111111")

		self.assertEqual(einvoice_be_api._extract_peppol_endpoints(b"<Invoice/>"), {})
		self.assertEqual(einvoice_be_api._extract_peppol_endpoints(b"not-xml"), {})

	def test_transmit_invoice_uploads_ubl_then_sends_with_query_params(self):
		client = einvoice_be_api.EInvoiceBeAPIClient("api-key")
		calls = []

		def fake_request(method, url, **kwargs):
			calls.append((method, url, kwargs))
			if url.endswith("/api/documents/ubl"):
				return MockHTTPResponse({"id": "doc-123", "state": "DRAFT"}, status_code=201)
			if url.endswith("/api/documents/doc-123/send"):
				return MockHTTPResponse({"id": "doc-123", "state": "TRANSIT"}, status_code=200)
			raise AssertionError(f"Unexpected request: {method} {url}")

		client.session.request = fake_request

		with patch(
			"edocument_integration_extention.einvoice_be_api.get_einvoice_be_client",
			return_value=client,
		):
			result = einvoice_be_api.transmit_invoice(
				BILLING_INVOICE_XML,
				invoice_doc=types.SimpleNamespace(name="EDOC-0001"),
				integration_settings={"api_key": "api-key"},
			)

		self.assertEqual(len(calls), 2)
		upload_files = calls[0][2]["files"]["file"]
		self.assertEqual(upload_files[0], "EDOC-0001.xml")
		self.assertEqual(upload_files[1], BILLING_INVOICE_XML)
		self.assertEqual(upload_files[2], "application/xml")

		self.assertEqual(
			calls[1][2]["params"],
			{
				"sender_peppol_scheme": "0208",
				"sender_peppol_id": "1018265814",
				"receiver_peppol_scheme": "0208",
				"receiver_peppol_id": "0848934496",
			},
		)
		self.assertEqual(result["document_id"], "doc-123")
		self.assertEqual(result["status"], "TRANSIT")
		self.assertEqual(result["recipient"], "0208:0848934496")

	def test_transmit_invoice_propagates_api_error(self):
		client = einvoice_be_api.EInvoiceBeAPIClient("api-key")
		client.session.request = Mock(
			return_value=MockHTTPResponse({"detail": "UBL file is not valid"}, status_code=415)
		)

		with patch(
			"edocument_integration_extention.einvoice_be_api.get_einvoice_be_client",
			return_value=client,
		):
			with self.assertRaises(einvoice_be_api.EInvoiceBeAPIError) as ctx:
				einvoice_be_api.transmit_invoice(
					BILLING_INVOICE_XML,
					integration_settings={"api_key": "api-key"},
				)

		self.assertIn("415", str(ctx.exception))
		self.assertIn("UBL file is not valid", str(ctx.exception))

	def test_download_document_ubl_fetches_signed_url_without_auth_header(self):
		client = einvoice_be_api.EInvoiceBeAPIClient("secret-key")
		client.session.request = Mock(
			return_value=MockHTTPResponse(
				{"id": "doc-1", "file_name": "doc-1.xml", "signed_url": "https://cdn.example.com/doc-1.xml?sig=x"}
			)
		)

		with patch("edocument_integration_extention.einvoice_be_api.requests.get") as mock_get:
			mock_get.return_value = MockHTTPResponse(None, status_code=200, content=b"<Invoice>ok</Invoice>")
			xml_bytes = client.download_document_ubl("doc-1")

		self.assertEqual(xml_bytes, b"<Invoice>ok</Invoice>")
		mock_get.assert_called_once_with(
			"https://cdn.example.com/doc-1.xml?sig=x",
			timeout=einvoice_be_api.UPLOAD_TIMEOUT_SECONDS,
		)
		self.assertNotIn("headers", mock_get.call_args.kwargs)

	def test_poll_inbox_paginates_skips_existing_and_handles_item_errors(self):
		fake_client = Mock()
		fake_client.get_inbox.side_effect = [
			{
				"items": [
					{"id": "doc-already", "state": "RECEIVED"},
					{"id": "doc-transit", "state": "TRANSIT"},
					{"id": "doc-new-1", "state": "RECEIVED"},
				],
				"has_next_page": True,
			},
			{
				"items": [
					{"id": "doc-broken", "state": "RECEIVED"},
					{"id": "doc-new-2", "state": "RECEIVED"},
				],
				"has_next_page": False,
			},
		]

		def fake_download(doc_id):
			if doc_id == "doc-broken":
				raise einvoice_be_api.EInvoiceBeAPIError("temporary download failure")
			return f"<Invoice>{doc_id}</Invoice>".encode()

		fake_client.download_document_ubl.side_effect = fake_download

		with (
			patch(
				"edocument_integration_extention.einvoice_be_api.get_einvoice_be_client",
				return_value=fake_client,
			),
			patch(
				"edocument_integration_extention.einvoice_be_api._get_existing_edocument_references",
				return_value={"doc-already"},
			),
		):
			result = einvoice_be_api.poll_inbox({"api_key": "api-key"})

		self.assertEqual(result["status"], "success")
		self.assertEqual([item["document_id"] for item in result["invoices"]], ["doc-new-1", "doc-new-2"])
		self.assertEqual(result["invoices"][0]["xml_bytes"], b"<Invoice>doc-new-1</Invoice>")
		self.assertEqual(fake_client.get_inbox.call_count, 2)
		self.assertEqual(self.mock_log_error.call_count, 1)

	def test_validate_connection_and_verify_recipient(self):
		client = einvoice_be_api.EInvoiceBeAPIClient("api-key")
		client.session.request = Mock(
			return_value=MockHTTPResponse(
				{"name": "Acme Tenant", "company_name": "Acme BV", "peppol_ids": ["0208:1018265814"]}
			)
		)
		with patch("edocument_integration_extention.einvoice_be_api.EInvoiceBeAPIClient", return_value=client):
			conn = einvoice_be_api.validate_einvoice_be_connection("api-key")
		self.assertEqual(conn["status"], "success")
		self.assertIn("Acme BV", conn["message"])

		lookup_client = einvoice_be_api.EInvoiceBeAPIClient()
		lookup_client.session.request = Mock(
			return_value=MockHTTPResponse(
				{
					"is_valid": True,
					"dns_valid": True,
					"business_card_valid": True,
					"supported_document_types": [
						"urn:oasis:names:specification:ubl:schema:xsd:Invoice-2::Invoice##urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0::2.1"
					],
					"business_card": {"name": "OpenPeppol VZW"},
				}
			)
		)
		with patch(
			"edocument_integration_extention.einvoice_be_api.EInvoiceBeAPIClient",
			return_value=lookup_client,
		):
			verified = einvoice_be_api.verify_recipient("0208:0848934496")
		self.assertTrue(verified["is_valid"])
		self.assertTrue(verified["can_receive"])
		self.assertEqual(verified["business_card"]["name"], "OpenPeppol VZW")

	def test_error_formatting_handles_detail_string_list_and_retry_after(self):
		client = einvoice_be_api.EInvoiceBeAPIClient("api-key")

		client.session.request = Mock(
			return_value=MockHTTPResponse(
				{"detail": [{"loc": ["body", "file"], "msg": "Field required", "type": "missing"}]},
				status_code=422,
			)
		)
		with self.assertRaises(einvoice_be_api.EInvoiceBeAPIError) as ctx_422:
			client.get_account_info()
		self.assertIn("body.file: Field required", str(ctx_422.exception))

		client.session.request = Mock(
			return_value=MockHTTPResponse(
				{"detail": "Too many requests"},
				status_code=429,
				headers={"Retry-After": "12"},
			)
		)
		with self.assertRaises(einvoice_be_api.EInvoiceBeAPIError) as ctx_429:
			client.get_account_info()
		self.assertIn("Retry-After: 12s", str(ctx_429.exception))

	def test_webhook_signature_matches_official_test_vector(self):
		# Vector from https://docs.e-invoice.be/essentials/webhooks.md
		event_payload = {
			"id": "evt-e7wyc7gtpqx4z73x2wqmwhebhbb3r8n3ovfhcsdbulxr3s2awf49de76yglrnri3",
			"tenant_id": "ten-k3m9x2p7q4w8z5n1",
			"created_at": 1762780249,
			"type": "document.sent",
			"data": {"document_id": "doc-1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d"},
			"text": "\u26a1\ufe0f New webhook event: document.sent\n\n{\n  \"document_id\": \"doc-1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d\"\n}",
		}
		expected_sig = "sha256=a0b509e3466347b9c227767ff860769c15bf2d6c975a5ae706813aeb22a8694e"

		# Non-canonical raw JSON (unsorted keys, unescaped unicode, compact separators) must still verify.
		raw_non_canonical = json.dumps(event_payload, sort_keys=False, ensure_ascii=False, separators=(",", ":")).encode(
			"utf-8"
		)

		self.assertEqual(einvoice_be_api.compute_webhook_signature(event_payload, "secret"), expected_sig)
		self.assertTrue(einvoice_be_api.verify_webhook_signature(raw_non_canonical, "secret", expected_sig))
		self.assertFalse(einvoice_be_api.verify_webhook_signature(raw_non_canonical, "wrong", expected_sig))
		self.assertFalse(einvoice_be_api.verify_webhook_signature(raw_non_canonical, "", expected_sig))
		self.assertFalse(einvoice_be_api.verify_webhook_signature(raw_non_canonical, "secret", None))

		tampered = dict(event_payload, tenant_id="ten-tampered")
		self.assertFalse(einvoice_be_api.verify_webhook_signature(tampered, "secret", expected_sig))


if __name__ == "__main__":
	unittest.main()
