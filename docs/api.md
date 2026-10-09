# API

Interactive schema: `http://localhost:8000/docs`. All endpoints start with `/api/v1`.

- POST `/auth/register`, `/auth/login`: email/password; sets a HttpOnly session cookie.
- GET `/auth/me`; POST `/auth/logout`.
- GET `/health`: mode and supported stack.
- GET/POST `/repositories`: list/register with `full_name` in owner/repo format.
- GET `/repositories/{id}`: owned repository.
- POST `/runs`: `repository_id`, `issue_text`, `base_ref` (default main), optional issue_number; returns 202. Issue number is currently accepted as metadata only; submit the issue text explicitly.
- GET `/runs`, `/runs/{id}`: persisted run state.
- GET `/runs/{id}/steps`, `/artifacts`, `/test-results`, `/pull-request`.
- GET `/runs/{id}/artifacts/{artifact_id}`: authorized text attachment.
- POST `/runs/{id}/cancel`: boundary cancellation, idempotent before publication.
- POST `/runs/{id}/approval`: `decision` approve/reject, optional comment; only while awaiting approval. Repeated matching decisions are idempotent.

401 means no valid session; 404 hides resources owned by another user; 409 indicates a conflicting state; 422 indicates invalid fields or unsupported input; 429 limits active runs. The owner acts as reviewer in this private MVP. No separate reviewer role or webhook handler is supplied.

## Live setup

- GET `/settings/readiness`: configuration/verification booleans, safe messages, model ID, provider endpoint, and whether the current account can configure the workspace. Never returns credentials.
- POST `/settings/connections`: optional `model`, `llm_api_key`, `github_token`; encrypted local storage; permanent workspace owner only; requires no active/pending runs. Blank secret fields preserve the existing secret.
- POST `/settings/verify`: one bounded real structured AI request plus a GitHub identity check; stores results associated with the current connection fingerprint. Changing credentials invalidates verification.
- POST `/auth/claim`: converts the temporary demo workspace owner to a permanent email/password while preserving its ID/history.

Live repository connection and task creation require the workspace owner. Live tasks additionally require verified connections and the Docker test image. Validation errors deliberately omit submitted values so malformed requests cannot echo passwords or tokens.
