# Optional curated-example priming

Priming lets an operator deliberately find representative examples before a
Cyclotron begins operating. It is optional. Runs and embedded stores still work
without it. Imported examples are trusted only after an operator explicitly
accepts complete items and supplies a configured label and explanation for each
selected classifier. Transformation feedback is a separate record.

There are two input routes:

- **Structured files:** Excel (`.xlsx`), CSV, JSON arrays and JSONL already contain
  item fields plus classifier-specific labels/explanations. They need no domain
  transformer. Preview reports row and field errors; any invalid row prevents
  acceptance of the whole batch. Templates are empty and derive columns and
  label choices from the configured item schema and pinned classifier revisions.
- **References or other domain inputs:** a configured external HTTP service
  transforms raw inputs into a complete candidate item. Customer code executes
  outside the Cyclotron host. The service may use Python, Tactus, TypeScript, or
  another implementation; the contract is HTTP, not a language plugin API.

The first domain integration is Papyrus reference curation: its existing
scholarly extraction yields title/abstract, while its web extraction and metadata
generation yield title/summary. Raw source artifacts remain private in the
customer service. Papyrus's GraphQL content models are not used to store source
bodies. No GraphQL-schema plugin framework is required.

## Structured rows and templates

A JSON/JSONL row contains the catalog item plus `labels`:

```json
{
  "id": "synthetic-example",
  "occurred_at": "2026-10-14",
  "values": {"title": "Synthetic title", "text": "Synthetic decision content"},
  "labels": {
    "editorial": {"label": "publish", "rationale": "Synthetic explicit human reason"},
    "scope": {"label": "in scope", "rationale": "Synthetic explicit human reason"}
  },
  "provenance": {"source": "synthetic test fixture"}
}
```

This is a synthetic example, not a suggested editorial label. File columns are
`id`, `occurred_at`, `values.<field>`, then `<classifier-id>.label` and
`<classifier-id>.rationale`. Objects, arrays, booleans and numbers in CSV cells
use JSON. Optional empty fields are omitted. With an unconstrained object schema,
use a single `values` column containing a JSON object.

A classifier configuration may declare `item_schema` (JSON Schema 2020-12), or
the host may supply a schema for an item list. Selected classifiers must agree
on that schema. Their existing `input_field` requirements are also checked.
Remote schema references are refused. The first slice bounds files to 4 MB and
batches to 200 rows, with an aggregate 8 MB draft limit. Excel formulas are
refused and never evaluated. A batch that expands beyond the draft limit reports
an explicit error and must be split.

Invalid imports never partially populate the catalog. Acceptance rechecks every
label/explanation, classifier revision, item identity and size inside the
transaction. It inserts the item and all labels together. Existing item identities
are refused; corrections use the existing catalog workflow. Acceptance retries
with the same content return the committed result rather than duplicate records.

## External transformation contract

The host registers an endpoint and configurable fields. Endpoints must use HTTPS;
explicit loopback HTTP is supported for local development. Endpoints, credentials
and tool permissions come from host configuration, never from fetched content.
The host owns authentication, egress policy, source permissions, budgets and
customer code execution. Cyclotron disables HTTP redirects and bounds response
sizes. It does not fetch source URLs itself or execute source instructions.

Start a job:

```http
POST /jobs
Idempotency-Key: <draft-id>
Content-Type: application/json
```

```json
{
  "contract_version": "cyclotron-transform/v1",
  "request_id": "<draft-id>",
  "inputs": {"reference": "<operator input>"},
  "item_schema": {"type": "object"},
  "classifiers": [{"id": "editorial", "input_field": "text"}]
}
```

The service returns `job_id` and one of `queued`, `running`, `clarification`,
`complete`, `failed`, or `cancelled`. `GET /jobs/<job-id>` returns current status.
A clarification response supplies `message` and `fields` with `name`, `label`,
optional `required` and `multiline`. A complete response supplies `candidate`:
`id`, `occurred_at`, `values`, and optional `provenance`. A URL reference alone is
not complete. Candidates must not contain classification `labels`.

Reject/revise or answer clarification:

```http
POST /jobs/<job-id>/feedback
Idempotency-Key: <draft-id>:<feedback-request-id>
```

```json
{"request_id": "<feedback-request-id>", "reason": "<transformation feedback>", "inputs": {}}
```

The service must retain the original job inputs and prior working state, apply
feedback, and return a new status. Human classification labels/explanations are
not sent to this endpoint. Start and feedback request identities must be
idempotent; conflicting reuse must fail. `DELETE /jobs/<job-id>` cancels the job
idempotently. Late results cannot make a locally cancelled draft acceptable.
If remote cancellation is uncertain, the local UI shows it as pending and offers
a cancellation retry. Reuse transformer IDs only for the same endpoint contract.

Cyclotron validates each completed candidate, shows its full **item values** for
every selected classifier, and requires human labels/explanations before local
acceptance. This is the target representation, not a fabricated prediction or a
claim to show a future provider's complete prompt. Raw input, file contents,
transformation feedback and the accepted target values remain auditable in the
private draft/catalog store. Source content renders as text, never executable
HTML. Configured credential values are refused in imported data; provider failures
are reported without copying private exception details into the UI.

## Standalone configuration

Install the optional web/priming dependencies and pass operator-owned JSON:

```sh
pip install -e '.[web]'
decision-flywheel-web serve --database var/web/workspace.sqlite3 \
  --priming-config /path/to/priming-host.json
```

Example host configuration (no credentials in the file):

```json
{
  "transformers": [{
    "id": "customer-domain-v1",
    "name": "Customer curation",
    "endpoint": "https://customer.example/curation",
    "authorization_env": "CUSTOMER_TRANSFORMER_AUTHORIZATION",
    "fields": [{"name": "reference", "label": "Reference", "required": true}]
  }],
  "schemas": {"example-list-id": {"type": "object"}}
}
```

The named environment value is the complete authorization header value, supplied
by the customer. This feature creates no credentials or IAM grants. The existing
GraphQL authentication/origin controls also protect priming queries and commands.

## Embedded UI and initial context

`cyclotron/components/priming-panel` exports `PrimingPanel` and its transport/types.
`cyclotron/priming-client` binds it to a host-supplied authenticated GraphQL
transport. A host can instead implement `PrimingClient` itself. The component
has no fixed API hostname, article fields, classifier count, or default labels.
`renderItem` lets a host show its own candidate presentation; the complete model
item values remain inspectable. `initialDraftId` and `onDraftChange` allow the
host to restore a durable draft after a connection loss.

The standalone item-list workspace offers **Import curated examples (optional)**.
Operators select the classifier revisions. New interactive runs may choose a
separate **Curated examples (optional)** item list. Accepted priming items and
operational item IDs must be disjoint. Priming is training context only; it does
not become held-out data, operational review counts, or invented predictions.
The run pins example values, labels, explanations and classifier revisions.

For a new embedded SDK store, obtain accepted examples with
`PrimingService.examples(list_id, classifier_refs)` and pass them as
`Cyclotron.open(..., priming_examples=examples)`. The store fixes them at creation,
persists them through reopen/snapshot, and rejects attempts to silently replace
them or decide an operational target with a priming identity. Normal creation
without `priming_examples` remains unchanged.

## Local review and remaining integration work

`scripts/priming_preview.py` runs the standalone synthetic preview on port 8796.
Papyrus's `scripts/priming-preview/` harness embeds the actual References view and
uses actual HTTP requests to the customer-service contract. It substitutes only
routing/cloud-auth and source/provider boundaries. Its verification checks paper
revision, web summarization, multiple explicit labels, one-output mobile
cancellation, and committed catalog/provenance records.

Ryan's real examples have not been supplied. Synthetic fixtures are labelled as
such. Public example content and production transport/hosting configuration need
separate review; these changes do not deploy an authenticated app at the marketing
hostname, register source content in production Papyrus, or publish examples.

Papyrus now supplies an explicit accepted-catalog snapshot -> optional new
relevance-store bootstrap (`papyrus_content.priming_bootstrap`). The shared panel
calls `onAccepted(draft)` with the acknowledged draft/list/classifier pins; the
host must still deliberately choose priming when creating a new store. Acceptance
does not start operations. No-argument callbacks remain usable by hosts that only
refresh their UI. This integration remains local until the existing priming API
and records are represented in the shared storage-owner conversion.
