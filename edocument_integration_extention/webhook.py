# Copyright (c) 2026, Dharshan and contributors
# For license information, please see license.txt

"""Webhook handler for e-invoice.be event notifications."""

import json
from typing import Any

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log

from edocument_integration_extention.einvoice_be_api import (
	PROVIDER_NAME,
	get_einvoice_be_client,
	integration_settings_from_doc,
	verify_webhook_signature,
)


def handle_einvoice_be_webhook(**kwargs):
	"""Validate X-Signature, verify tenant settings, and process an e-invoice.be webhook."""
	request_log = None
	try:
		request = frappe.request
		if not request:
			return _webhook_response({"status": "error", "message": "No request data received"}, 400)

		raw_body = request.get_data()
		if not raw_body:
			return _webhook_response({"status": "error", "message": "No content found in request"}, 400)

		notification = _parse_json_dict(raw_body)
		if notification is None:
			return _webhook_response({"status": "error", "message": "Invalid webhook payload"}, 400)

		signature = request.headers.get("X-Signature")
		if not signature:
			return _webhook_response({"status": "error", "message": "Missing X-Signature header"}, 401)

		request_log = create_request_log(
			notification or kwargs,
			request_description="Incoming e-invoice.be Webhook",
			service_name=PROVIDER_NAME,
			request_headers=_sanitize_headers(request.headers),
		)

		settings_doc = _find_authenticated_settings(notification, signature)
		if not settings_doc:
			request_log.status = "Failed"
			request_log.error = "Invalid webhook signature"
			return _webhook_response({"status": "error", "message": "Invalid webhook signature"}, 401)

		result, status_code = _dispatch_event(settings_doc, notification)
		request_log.status = "Completed" if status_code < 400 else "Failed"
		request_log.output = frappe.as_json(result)
		if status_code >= 400:
			return _webhook_response({"status": "error", "message": result["message"]}, status_code)
		return _webhook_response({"status": "success", "result": result}, status_code)

	except Exception as exc:
		if request_log:
			request_log.status = "Failed"
			request_log.error = frappe.get_traceback()
		frappe.db.rollback()
		frappe.log_error(
			title="e-invoice.be Webhook Error",
			message=f"e-invoice.be webhook failed: {exc!s}\n{frappe.get_traceback()}",
		)
		frappe.db.commit()  # nosemgrep: Persist failure log before returning error
		return _webhook_response({"status": "error", "message": "Internal server error"}, 500)
	finally:
		if request_log and frappe.db.exists("Integration Request", request_log.name):
			request_log.save(ignore_permissions=True)


def _dispatch_event(settings_doc, notification: dict[str, Any]) -> tuple[dict[str, Any], int]:
	event_type = notification.get("type")
	data = notification.get("data") if isinstance(notification.get("data"), dict) else {}
	document_id = data.get("document_id")

	if event_type == "document.received":
		if not document_id:
			return {"message": "Missing document_id in document.received event"}, 400
		return _handle_document_received(settings_doc, document_id), 200

	if event_type == "document.sent":
		edocument_name = _annotate_outgoing_edocument(
			document_id,
			_("e-invoice.be confirmed the document was sent ({0})").format(document_id),
		)
		return {"event": event_type, "document_id": document_id, "edocument": edocument_name}, 200

	if event_type == "document.sent.failed":
		edocument_name = _mark_outgoing_edocument_failed(document_id)
		return {"event": event_type, "document_id": document_id, "edocument": edocument_name}, 200

	if event_type == "document.received.failed":
		frappe.log_error(
			title="e-invoice.be Webhook Receive Failed",
			message=f"e-invoice.be failed to process inbound document {document_id}",
		)
		return {"event": event_type, "document_id": document_id, "edocument": None}, 200

	return {"event": event_type, "document_id": document_id, "edocument": None}, 200


def _handle_document_received(settings_doc, document_id: str) -> dict[str, Any]:
	existing = frappe.db.exists("EDocument", {"reference": document_id})
	if existing:
		return {"edocument": existing, "skipped": True, "reason": "duplicate", "document_id": document_id}

	integration_settings = integration_settings_from_doc(settings_doc)
	xml_bytes = get_einvoice_be_client(integration_settings).download_document_ubl(document_id)
	imported = settings_doc.process_incoming_document(xml_bytes, document_id)
	imported["document_id"] = document_id
	return imported


def _annotate_outgoing_edocument(document_id: str | None, comment_text: str) -> str | None:
	if not document_id:
		return None
	edocument_name = frappe.db.get_value("EDocument", {"reference": document_id, "direction": "Outgoing"})
	if not edocument_name:
		return None
	frappe.get_doc("EDocument", edocument_name).add_comment("Info", comment_text)
	frappe.db.commit()  # nosemgrep: Webhook update must persist before returning
	return edocument_name


def _mark_outgoing_edocument_failed(document_id: str | None) -> str | None:
	if not document_id:
		return None
	edocument_name = frappe.db.get_value("EDocument", {"reference": document_id, "direction": "Outgoing"})
	if not edocument_name:
		return None

	edocument = frappe.get_doc("EDocument", edocument_name)
	error_text = _("e-invoice.be reported delivery failure for document {0}").format(document_id)
	edocument.add_comment("Info", error_text)
	edocument.reload()
	edocument.status = "Transmission Failed"
	edocument.error = error_text
	edocument.save(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep: Webhook update must persist before returning
	return edocument_name


def _find_authenticated_settings(notification: dict[str, Any], signature: str):
	tenant_id = notification.get("tenant_id")
	rows = frappe.get_all(
		"EDocument Integration Settings",
		filters={"edocument_integrator": PROVIDER_NAME},
		fields=["name", "account_id"],
	)
	if not rows:
		return None

	# Check settings matching tenant_id first, then fall back to other configured e-invoice.be rows.
	ordered = sorted(rows, key=lambda r: 0 if (tenant_id and r.get("account_id") == tenant_id) else 1)
	for row in ordered:
		settings_doc = frappe.get_doc("EDocument Integration Settings", row["name"])
		secret = settings_doc.get_password("webhook_secret", raise_exception=False)
		if secret and verify_webhook_signature(notification, secret, signature):
			return settings_doc
	return None


def _parse_json_dict(raw_body: bytes | str) -> dict[str, Any] | None:
	try:
		text = raw_body.decode("utf-8") if isinstance(raw_body, bytes | bytearray) else raw_body
		parsed = json.loads(text)
	except (UnicodeDecodeError, json.JSONDecodeError):
		return None
	return parsed if isinstance(parsed, dict) else None


def _sanitize_headers(headers) -> dict[str, str]:
	return {k: v for k, v in dict(headers).items() if k.lower() != "authorization"}


def _webhook_response(payload: dict[str, Any], status_code: int) -> tuple[dict[str, Any], int]:
	response = getattr(frappe.local, "response", None)
	if isinstance(response, dict):
		response["http_status_code"] = status_code
	return payload, status_code
