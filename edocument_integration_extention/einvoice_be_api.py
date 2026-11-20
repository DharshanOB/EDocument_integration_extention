# Copyright (c) 2026, Dharshan and contributors
# For license information, please see license.txt

"""e-invoice.be Peppol Access Point API client and integration helpers."""

import hashlib
import hmac
import json
from typing import Any

import frappe
import requests
from lxml import etree as ET

PROVIDER_NAME = "e-invoice.be"
DEFAULT_BASE_URL = "https://api.e-invoice.be"
DEFAULT_TIMEOUT_SECONDS = 30
UPLOAD_TIMEOUT_SECONDS = 60
SELF_BILLING_CUSTOMIZATION_ID = (
	"urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:selfbilling:3.0"
)
INVOICE_DOC_TYPE_PREFIX = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2::Invoice##"

DEFAULT_UBL_NAMESPACES = {
	"ubl": "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2",
	"cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
	"cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
}


class EInvoiceBeAPIError(Exception):
	"""Custom exception for e-invoice.be API errors."""


class EInvoiceBeAPIClient:
	"""REST API client for the e-invoice.be Peppol Access Point."""

	def __init__(self, api_key: str | None = None, base_url: str | None = None):
		self.api_key = api_key
		self.base_url = (base_url or DEFAULT_BASE_URL).strip().rstrip("/")
		self.session = requests.Session()
		headers = {
			"Accept": "application/json",
			"User-Agent": "Frappe-EDocument/1.0",
		}
		if api_key:
			headers["Authorization"] = f"Bearer {api_key}"
		# Never set a session-wide Content-Type so multipart uploads supply their own boundary.
		self.session.headers.update(headers)

	def _request(
		self,
		method: str,
		endpoint: str,
		timeout: int = DEFAULT_TIMEOUT_SECONDS,
		**kwargs,
	) -> dict[str, Any] | list[Any]:
		url = f"{self.base_url}{endpoint}"
		try:
			response = self.session.request(method, url, timeout=timeout, **kwargs)
		except requests.exceptions.RequestException as exc:
			error_msg = f"e-invoice.be API request failed: {exc!s}"
			frappe.log_error(title="e-invoice.be API Error", message=error_msg)
			raise EInvoiceBeAPIError(error_msg) from exc

		if not response.ok:
			error_msg = _format_error_response(response)
			frappe.log_error(title="e-invoice.be API Error", message=error_msg)
			raise EInvoiceBeAPIError(error_msg)

		if not response.content:
			return {}

		try:
			return response.json()
		except json.JSONDecodeError as exc:
			error_msg = f"e-invoice.be API returned invalid JSON: {response.text}"
			frappe.log_error(title="e-invoice.be API Error", message=error_msg)
			raise EInvoiceBeAPIError(error_msg) from exc

	def get_account_info(self) -> dict[str, Any]:
		result = self._request("GET", "/api/me/")
		if not isinstance(result, dict):
			raise EInvoiceBeAPIError("Unexpected response from /api/me/")
		return result

	def validate_ubl(self, xml_content: str | bytes, filename: str = "invoice.xml") -> dict[str, Any]:
		xml_bytes = _to_bytes(xml_content)
		result = self._request(
			"POST",
			"/api/validate/ubl",
			files={"file": (filename, xml_bytes, "application/xml")},
			timeout=UPLOAD_TIMEOUT_SECONDS,
		)
		if not isinstance(result, dict):
			raise EInvoiceBeAPIError("Unexpected response from /api/validate/ubl")
		return result

	def create_document_from_ubl(
		self, xml_content: str | bytes, filename: str = "invoice.xml"
	) -> dict[str, Any]:
		xml_bytes = _to_bytes(xml_content)
		result = self._request(
			"POST",
			"/api/documents/ubl",
			files={"file": (filename, xml_bytes, "application/xml")},
			timeout=UPLOAD_TIMEOUT_SECONDS,
		)
		if not isinstance(result, dict):
			raise EInvoiceBeAPIError("Unexpected response from /api/documents/ubl")
		return result

	def send_document(
		self,
		document_id: str,
		sender_peppol_scheme: str | None = None,
		sender_peppol_id: str | None = None,
		receiver_peppol_scheme: str | None = None,
		receiver_peppol_id: str | None = None,
	) -> dict[str, Any]:
		raw_params = {
			"sender_peppol_scheme": sender_peppol_scheme,
			"sender_peppol_id": sender_peppol_id,
			"receiver_peppol_scheme": receiver_peppol_scheme,
			"receiver_peppol_id": receiver_peppol_id,
		}
		params = {key: value for key, value in raw_params.items() if value}
		result = self._request("POST", f"/api/documents/{document_id}/send", params=params or None)
		if not isinstance(result, dict):
			raise EInvoiceBeAPIError("Unexpected response from send document endpoint")
		return result

	def get_document(self, document_id: str) -> dict[str, Any]:
		result = self._request("GET", f"/api/documents/{document_id}")
		if not isinstance(result, dict):
			raise EInvoiceBeAPIError("Unexpected response from get document endpoint")
		return result

	def get_document_ubl(self, document_id: str) -> dict[str, Any]:
		result = self._request("GET", f"/api/documents/{document_id}/ubl")
		if not isinstance(result, dict):
			raise EInvoiceBeAPIError("Unexpected response from get document UBL endpoint")
		return result

	def download_document_ubl(self, document_id: str) -> bytes:
		ubl_meta = self.get_document_ubl(document_id)
		signed_url = ubl_meta.get("signed_url")
		if not isinstance(signed_url, str) or not signed_url.startswith(("http://", "https://")):
			raise EInvoiceBeAPIError(f"Missing or invalid signed_url for document {document_id}")

		# Signed URLs carry self-contained auth; sending Authorization can break object-store validation.
		try:
			response = requests.get(signed_url, timeout=UPLOAD_TIMEOUT_SECONDS)
		except requests.exceptions.RequestException as exc:
			error_msg = f"Failed to download UBL from signed_url for {document_id}: {exc!s}"
			frappe.log_error(title="e-invoice.be Download Error", message=error_msg)
			raise EInvoiceBeAPIError(error_msg) from exc

		if not response.ok:
			error_msg = f"UBL download failed ({response.status_code}) for document {document_id}"
			frappe.log_error(title="e-invoice.be Download Error", message=error_msg)
			raise EInvoiceBeAPIError(error_msg)

		if not response.content:
			raise EInvoiceBeAPIError(f"Downloaded UBL content is empty for document {document_id}")

		return response.content

	def get_document_lifecycle(self, document_id: str) -> dict[str, Any]:
		result = self._request("GET", f"/api/documents/{document_id}/lifecycle")
		if not isinstance(result, dict):
			raise EInvoiceBeAPIError("Unexpected response from document lifecycle endpoint")
		return result

	def get_document_timeline(self, document_id: str) -> dict[str, Any]:
		result = self._request("GET", f"/api/documents/{document_id}/timeline")
		if not isinstance(result, dict):
			raise EInvoiceBeAPIError("Unexpected response from document timeline endpoint")
		return result

	def get_inbox(
		self,
		page: int = 1,
		page_size: int = 100,
		doc_type: str | None = None,
		sender: str | None = None,
		date_from: str | None = None,
		date_to: str | None = None,
		search: str | None = None,
		sort_by: str = "created_at",
		sort_order: str = "desc",
	) -> dict[str, Any]:
		params = _build_listing_params(
			page=page,
			page_size=page_size,
			doc_type=doc_type,
			date_from=date_from,
			date_to=date_to,
			search=search,
			sort_by=sort_by,
			sort_order=sort_order,
			sender=sender,
		)
		result = self._request("GET", "/api/inbox/", params=params)
		if not isinstance(result, dict):
			raise EInvoiceBeAPIError("Unexpected response from /api/inbox/")
		return result

	def get_outbox(
		self,
		page: int = 1,
		page_size: int = 100,
		doc_type: str | None = None,
		receiver: str | None = None,
		date_from: str | None = None,
		date_to: str | None = None,
		search: str | None = None,
		sort_by: str = "created_at",
		sort_order: str = "desc",
	) -> dict[str, Any]:
		params = _build_listing_params(
			page=page,
			page_size=page_size,
			doc_type=doc_type,
			date_from=date_from,
			date_to=date_to,
			search=search,
			sort_by=sort_by,
			sort_order=sort_order,
			receiver=receiver,
		)
		result = self._request("GET", "/api/outbox/", params=params)
		if not isinstance(result, dict):
			raise EInvoiceBeAPIError("Unexpected response from /api/outbox/")
		return result

	def validate_peppol_id(self, peppol_id: str) -> dict[str, Any]:
		result = self._request("GET", "/api/validate/peppol-id", params={"peppol_id": peppol_id})
		if not isinstance(result, dict):
			raise EInvoiceBeAPIError("Unexpected response from /api/validate/peppol-id")
		return result

	def lookup_peppol_id(self, peppol_id: str) -> dict[str, Any]:
		result = self._request("GET", "/api/lookup", params={"peppol_id": peppol_id})
		if not isinstance(result, dict):
			raise EInvoiceBeAPIError("Unexpected response from /api/lookup")
		return result

	def search_participants(self, query: str, country_code: str | None = None) -> dict[str, Any]:
		params: dict[str, Any] = {"query": query}
		if country_code:
			params["country_code"] = country_code
		result = self._request("GET", "/api/lookup/participants", params=params)
		if not isinstance(result, dict):
			raise EInvoiceBeAPIError("Unexpected response from /api/lookup/participants")
		return result

	def list_webhooks(self) -> list[dict[str, Any]]:
		result = self._request("GET", "/api/webhooks/")
		if not isinstance(result, list):
			raise EInvoiceBeAPIError("Unexpected response from /api/webhooks/")
		return result

	def create_webhook(self, url: str, events: list[str], enabled: bool = True) -> dict[str, Any]:
		result = self._request(
			"POST",
			"/api/webhooks/",
			json={"url": url, "events": events, "enabled": enabled},
		)
		if not isinstance(result, dict):
			raise EInvoiceBeAPIError("Unexpected response from create webhook endpoint")
		return result

	def delete_webhook(self, webhook_id: str) -> dict[str, Any]:
		result = self._request("DELETE", f"/api/webhooks/{webhook_id}")
		if not isinstance(result, dict):
			raise EInvoiceBeAPIError("Unexpected response from delete webhook endpoint")
		return result


def integration_settings_from_doc(settings_doc) -> dict[str, Any]:
	"""Return integration settings dict without raising when api_secret is unset."""
	return {
		"api_key": settings_doc.api_key,
		"api_secret": settings_doc.get_password("api_secret", raise_exception=False),
		"base_url": settings_doc.base_url,
		"edocument_integrator": settings_doc.edocument_integrator,
		"company": settings_doc.company,
		"account_id": settings_doc.account_id,
		"company_id": settings_doc.company_id,
	}


def get_einvoice_be_client(integration_settings: dict[str, Any]) -> EInvoiceBeAPIClient:
	if not integration_settings or not isinstance(integration_settings, dict):
		raise EInvoiceBeAPIError("e-invoice.be integration settings must be provided as a dict")

	api_key = integration_settings.get("api_key")
	if not api_key:
		raise EInvoiceBeAPIError("e-invoice.be API key not configured")

	base_url = (integration_settings.get("base_url") or DEFAULT_BASE_URL).strip()
	return EInvoiceBeAPIClient(api_key=api_key, base_url=base_url)


def transmit_invoice(
	xml_content: str | bytes,
	invoice_doc=None,
	integration_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
	client = get_einvoice_be_client(integration_settings)
	xml_bytes = _to_bytes(xml_content)
	filename = f"{getattr(invoice_doc, 'name', None) or 'invoice'}.xml"
	endpoints = _extract_peppol_endpoints(xml_bytes)

	created = client.create_document_from_ubl(xml_bytes, filename=filename)
	document_id = created.get("id")
	if not document_id:
		raise EInvoiceBeAPIError("e-invoice.be did not return a document ID when creating from UBL")

	sent = client.send_document(
		document_id,
		sender_peppol_scheme=endpoints.get("sender_peppol_scheme"),
		sender_peppol_id=endpoints.get("sender_peppol_id"),
		receiver_peppol_scheme=endpoints.get("receiver_peppol_scheme"),
		receiver_peppol_id=endpoints.get("receiver_peppol_id"),
	)

	result: dict[str, Any] = {
		"status": sent.get("state") or "TRANSIT",
		"id": document_id,
		"document_id": document_id,
		"tracking_id": document_id,
		"response": sent,
	}
	if endpoints.get("sender"):
		result["sender"] = endpoints["sender"]
	if endpoints.get("recipient"):
		result["recipient"] = endpoints["recipient"]
	return result


def poll_inbox(
	integration_settings: dict[str, Any] | None = None,
	company_id: str | None = None,
	existing_ids: set[str] | None = None,
	max_pages: int = 50,
) -> dict[str, Any]:
	del company_id
	client = get_einvoice_be_client(integration_settings)
	known_ids = set(existing_ids or ())
	invoices: list[dict[str, Any]] = []

	for page in range(1, max_pages + 1):
		inbox_page = client.get_inbox(page=page, page_size=100, sort_by="created_at", sort_order="desc")
		items = inbox_page.get("items") or []
		if not items:
			break

		page_ids = [doc.get("id") for doc in items if doc.get("id")]
		known_ids.update(_get_existing_edocument_references(page_ids) - known_ids)

		for doc in items:
			document_id = doc.get("id")
			if not document_id:
				continue
			state = doc.get("state")
			if state and state != "RECEIVED":
				continue
			if document_id in known_ids:
				continue

			try:
				xml_bytes = client.download_document_ubl(document_id)
			except Exception as exc:
				frappe.log_error(
					title="e-invoice.be Inbox Polling Error",
					message=f"Failed to download UBL for {document_id}: {exc!s}\n{frappe.get_traceback()}",
				)
				continue

			known_ids.add(document_id)
			invoices.append({"xml_bytes": xml_bytes, "document_id": document_id, "metadata": doc})

		if not inbox_page.get("has_next_page"):
			break

	message = f"Found {len(invoices)} invoice(s)" if invoices else "No new invoices found"
	return {"status": "success", "message": message, "invoices": invoices}


def validate_einvoice_be_connection(api_key: str, base_url: str | None = None) -> dict[str, Any]:
	try:
		client = EInvoiceBeAPIClient(api_key=api_key, base_url=base_url)
		tenant = client.get_account_info()
		company_name = tenant.get("company_name") or tenant.get("name") or "e-invoice.be tenant"
		return {
			"status": "success",
			"message": f"e-invoice.be API connection successful for {company_name}",
			"tenant": tenant,
		}
	except Exception as exc:
		return {"status": "error", "message": f"e-invoice.be API connection failed: {exc!s}"}


def verify_recipient(
	peppol_id: str, integration_settings: dict[str, Any] | None = None
) -> dict[str, Any]:
	settings = integration_settings or {}
	client = EInvoiceBeAPIClient(
		api_key=settings.get("api_key"),
		base_url=settings.get("base_url"),
	)
	result = client.validate_peppol_id(peppol_id)
	supported_types = result.get("supported_document_types") or []
	can_receive = bool(
		result.get("is_valid")
		and any(isinstance(doc_type, str) and doc_type.startswith(INVOICE_DOC_TYPE_PREFIX) for doc_type in supported_types)
	)
	return {
		"status": "success",
		"peppol_id": peppol_id,
		"is_valid": bool(result.get("is_valid")),
		"dns_valid": bool(result.get("dns_valid")),
		"business_card_valid": bool(result.get("business_card_valid")),
		"supported_document_types": supported_types,
		"business_card": result.get("business_card"),
		"can_receive": can_receive,
	}


def compute_webhook_signature(payload: dict[str, Any] | bytes | str, secret: str) -> str:
	if isinstance(payload, bytes | bytearray):
		data = json.loads(payload.decode("utf-8"))
	elif isinstance(payload, str):
		data = json.loads(payload)
	else:
		data = payload

	# Canonical JSON per e-invoice.be spec matches json.dumps(payload, sort_keys=True) defaults.
	canonical = json.dumps(data, sort_keys=True)
	digest = hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
	return f"sha256={digest}"


def verify_webhook_signature(
	payload: dict[str, Any] | bytes | str,
	secret: str | None,
	signature_header: str | None,
) -> bool:
	if not secret or not signature_header:
		return False
	try:
		expected = compute_webhook_signature(payload, secret)
	except (TypeError, ValueError, UnicodeDecodeError):
		return False
	return hmac.compare_digest(signature_header.strip().encode("utf-8"), expected.encode("utf-8"))


def _to_bytes(xml_content: str | bytes) -> bytes:
	if isinstance(xml_content, bytes):
		return xml_content
	return xml_content.encode("utf-8")


def _get_ubl_namespaces() -> dict[str, str]:
	try:
		from edocument.edocument.profiles.peppol import UBL_NAMESPACES

		return UBL_NAMESPACES
	except ImportError:
		return DEFAULT_UBL_NAMESPACES


def _parse_endpoint_element(elem) -> tuple[str | None, str | None]:
	if elem is None or not elem.text:
		return None, None
	scheme = (elem.get("schemeID") or "").strip() or None
	value = elem.text.strip()
	if not value:
		return None, None
	if scheme and value.startswith(f"{scheme}:"):
		value = value[len(scheme) + 1 :]
	elif not scheme and ":" in value:
		scheme, value = value.split(":", 1)
	if not scheme or not value:
		return None, None
	return scheme, value


def _extract_peppol_endpoints(xml_bytes: bytes) -> dict[str, str]:
	try:
		root = ET.fromstring(xml_bytes)
	except ET.ParseError:
		return {}

	namespaces = _get_ubl_namespaces()
	supplier_elem = root.find(".//cac:AccountingSupplierParty/cac:Party/cbc:EndpointID", namespaces)
	customer_elem = root.find(".//cac:AccountingCustomerParty/cac:Party/cbc:EndpointID", namespaces)
	customization_id = (root.findtext(".//cbc:CustomizationID", namespaces=namespaces) or "").strip()

	sender_elem, receiver_elem = supplier_elem, customer_elem
	if customization_id == SELF_BILLING_CUSTOMIZATION_ID:
		sender_elem, receiver_elem = customer_elem, supplier_elem

	sender_scheme, sender_id = _parse_endpoint_element(sender_elem)
	receiver_scheme, receiver_id = _parse_endpoint_element(receiver_elem)

	endpoints: dict[str, str] = {}
	if sender_scheme and sender_id:
		endpoints["sender_peppol_scheme"] = sender_scheme
		endpoints["sender_peppol_id"] = sender_id
		endpoints["sender"] = f"{sender_scheme}:{sender_id}"
	if receiver_scheme and receiver_id:
		endpoints["receiver_peppol_scheme"] = receiver_scheme
		endpoints["receiver_peppol_id"] = receiver_id
		endpoints["recipient"] = f"{receiver_scheme}:{receiver_id}"
	return endpoints


def _build_listing_params(
	*,
	page: int,
	page_size: int,
	doc_type: str | None,
	date_from: str | None,
	date_to: str | None,
	search: str | None,
	sort_by: str,
	sort_order: str,
	**extra: str | None,
) -> dict[str, Any]:
	params: dict[str, Any] = {
		"page": max(page, 1),
		"page_size": min(max(page_size, 1), 100),
		"sort_by": sort_by,
		"sort_order": sort_order,
	}
	optional = {
		"type": doc_type,
		"date_from": date_from,
		"date_to": date_to,
		"search": search,
		**extra,
	}
	for key, value in optional.items():
		if value:
			params[key] = value
	return params


def _format_error_response(response: requests.Response) -> str:
	detail_text = response.text
	try:
		payload = response.json()
	except json.JSONDecodeError:
		payload = None

	if isinstance(payload, dict) and "detail" in payload:
		detail = payload["detail"]
		if isinstance(detail, str):
			detail_text = detail
		elif isinstance(detail, list):
			parts = []
			for item in detail:
				if isinstance(item, dict):
					loc = ".".join(str(x) for x in (item.get("loc") or []))
					msg = item.get("msg") or json.dumps(item)
					parts.append(f"{loc}: {msg}" if loc else str(msg))
				else:
					parts.append(str(item))
			detail_text = "; ".join(parts)
		else:
			detail_text = json.dumps(detail)
	elif payload is not None:
		detail_text = json.dumps(payload)

	msg = f"e-invoice.be API error ({response.status_code}): {detail_text}"
	retry_after = response.headers.get("Retry-After") if response.status_code in (429, 503) else None
	if retry_after:
		msg = f"{msg} (Retry-After: {retry_after}s)"
	return msg


def _get_existing_edocument_references(document_ids: list[str]) -> set[str]:
	if not document_ids or not getattr(frappe, "db", None):
		return set()
	try:
		rows = frappe.get_all(
			"EDocument",
			filters={"reference": ["in", document_ids]},
			pluck="reference",
		)
		return {row for row in rows if row}
	except Exception:
		return set()
