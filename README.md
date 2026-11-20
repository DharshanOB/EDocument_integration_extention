# EDocument Integration Extention

Frappe custom app extending [`edocument_integration`](https://github.com/prilk-consulting/edocument_integration) with **[e-invoice.be](https://e-invoice.be/)** Peppol Access Point support without modifying `edocument` or `edocument_integration`.

## Features

- **Provider selection**: Adds `e-invoice.be` to the **EDocument Integrator** field on **EDocument Integration Settings** via `Property Setter` and adds a **Webhook Secret** field (`webhook_secret`).
- **Outgoing UBL transmission**: Hooks `edocument_integration.api.transmit_edocument` to upload UBL XML via `POST /api/documents/ubl` and send via `POST /api/documents/{document_id}/send` with explicit `sender_peppol_scheme`, `sender_peppol_id`, `receiver_peppol_scheme`, and `receiver_peppol_id` query parameters (including self-billing direction handling).
- **Incoming inbox polling**: Extends `EDocumentIntegrationSettings.poll_incoming_documents` to paginate `GET /api/inbox/`, skip already-imported `EDocument` references, fetch temporary `signed_url` links via `GET /api/documents/{document_id}/ubl`, download UBL XML without passing Bearer credentials to the signed URL, and import records through `EDocument`. Works with both the **Poll Incoming Documents** button and the hourly `edocument_integration.tasks.poll_all_incoming_documents` scheduler job.
- **Webhook handling**: Hooks `edocument_integration.api.webhook` and exposes `edocument_integration_extention.api.einvoice_be_webhook` with canonical-JSON HMAC-SHA256 `X-Signature` verification (`sha256=...`) for:
  - `document.received` (downloads UBL from e-invoice.be API, deduplicates by `reference`, imports `EDocument`)
  - `document.sent` (annotates outgoing `EDocument`)
  - `document.sent.failed` (marks outgoing `EDocument` as `Transmission Failed`)
  - `document.received.failed` (logs error in Frappe)
- **Desk utilities**:
  - **Test Connection** (`GET /api/me/`)
  - **Verify Peppol ID** (`GET /api/validate/peppol-id`)
  - **UBL Validation API** (`edocument_integration_extention.api.validate_edocument_with_einvoice_be` -> `POST /api/validate/ubl`)

## Setup

1. Install the app on your site (requires `edocument` and `edocument_integration`):
   ```bash
   bench --site your-site install-app edocument_integration_extention
   bench --site your-site migrate
   ```
2. Open **EDocument Integration Settings** and create a record:
   - **EDocument Profile**: `PEPPOL`
   - **Company**: Your company
   - **EDocument Integrator**: `e-invoice.be`
   - **API Key**: Your e-invoice.be company API key (sandbox or production)
   - **Base URL**: `https://api.e-invoice.be` (auto-filled)
   - **Account ID**: Optional e-invoice.be tenant ID (`ten-...`) for faster multi-tenant webhook matching
   - **Webhook Secret**: Optional signing secret returned by `POST /api/webhooks/` on e-invoice.be
3. Configure your webhook in e-invoice.be pointing to either:
   - `https://your-domain.com/api/method/edocument_integration_extention.api.einvoice_be_webhook`
   - `https://your-domain.com/api/method/edocument_integration.api.webhook`

## License

MIT
