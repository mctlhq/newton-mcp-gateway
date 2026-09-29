# Archetype integration: confirmed, proposed, and open questions

> This document separates what is confirmed by Archetype AI's public documentation
> (docs.archetypeai.app, indexed at `/llms.txt`) from what this project proposes on top of it.
> The Physical Action Contract and the action runtime described in Part 2 are **this project's
> experimental proposal**, not an Archetype standard, and every result attributed to them is
> **mock-validated** only -- no live Newton account and no live MCP actuator server has been
> exercised. This project is not affiliated with or endorsed by Archetype AI. No private endpoint
> was reverse-engineered and no access control was circumvented to write this document or the code
> it describes -- see the closing statement, below.

## Part 1: Confirmed from Archetype's public documentation

Every row below cites a concrete page under `docs.archetypeai.app/`, not the site root, and is
restricted to an endpoint, field, event type, model family, or environment variable name this
repository's code already relies on.

| Item | Confirmed detail | Source |
|---|---|---|
| Direct Query endpoint | `POST {ATAI_API_ENDPOINT}/query` | `api-reference/query.md` |
| `/query` request fields | `model`, `query`, `system_prompt`, `instruction_prompt`, `file_ids`, `events`, `max_new_tokens`, `normalize_input`, `sanitize_response` | `api-reference/query.md` |
| `/query` response fields | `response.response`, `status`, `query_id`, `inference_time_sec`, `error_msg`, `errors`, `detail` (on a 401) | `api-reference/query.md` |
| `DataEvent` shape | `{type, event_data}`; for most event types `event_data.contents` carries the payload | `core-concepts/streams/events/data-events.md` (marked archived; the current `/llms.txt` states its payload shapes are the ones Direct Query uses in `events`) |
| Event types | `data.text`, `data.json`, `data.base64_img`, `data.base64_img_array`, `data.numeric_array` | `core-concepts/streams/events/data-events.md` |
| Files API upload | `POST /v0.5/files`, `multipart/form-data`, response `is_valid` / `file_id` / `file_uid`, ceiling **512 MB** | `api-reference/files/upload.md` |
| Files API base64 upload | `POST /v0.5/files/base64`, multipart form field `file` holding base64 text | `api-reference/files/upload-base64.md` |
| Model families | Newton C (`Newton::c2_...`, text/image/video reasoning, structured output); Omega encoders (`OmegaEncoder::...`, time-series to a 768-dim embedding per channel) | `api-reference/query.md` |
| Environment variable names | `ATAI_API_KEY`, `ATAI_API_ENDPOINT` | `libraries/environment-variables.md` |
| Agents API concepts | blueprint -> bundle -> run, with paginated results/events/logs | `core-concepts/agents/overview.md`, `core-concepts/agents/api.md` |
| Agents API blueprints (five) | `osm`, `anomaly-discovery`, `rare-event-detection`, `task-verification`, `manual-generation` | `core-concepts/agents/{osm,anomaly-discovery,rare-event-detection,task-verification,manual-generation}.md` (one page per blueprint) |
| Agents API operations | create a blueprint, create a bundle, run a bundle, get results, list events, get logs, list the node registry | `api-reference/agents/{create-blueprint,create-bundle,run-bundle,get-agent-results,list-agent-events,get-agent-logs,list-node-registry}.md` |

The Agents API is **not wired into this gateway** -- no MCP tool in `src/newton_mcp/server.py`
calls it. `mctlhq/newton-mcp-gateway#13` tracks that work, deferred until after first contact with
an Archetype engineer.

**`task-verification`, specifically.** Its public page describes the Task Verification Agent as
observing video data to confirm that work was performed according to a standard operating
procedure. Whether it is suitable for verifying that a *commanded physical outcome* occurred --
the question this repository's own verifier (`src/newton_mcp/runtime/verifier.py`) answers today
by polling a capability's `read_tool` -- is **not confirmed by any public page**. See Part 3,
question 4, below.

No endpoint, parameter, or model id appears anywhere in this document, or in this repository's
code, that is not already present in `src/` or in `docs/newton-api-notes.md` with a cited public
doc page.

## Part 2: Proposed by this project

Everything below is this project's own design, not an Archetype standard. Each row names the
module that owns it and repeats the mock-validated status.

| Item | Module | Status |
|---|---|---|
| Physical Action Contract v0.2 | `src/newton_mcp/action/contract.py`, `conditions.py` | Mock-validated schema; not an Archetype standard. |
| `runtime.yaml` capability allow-list and catalog | `src/newton_mcp/runtime/config.py`, `runtime/catalog.py` | Mock-validated discovery only (`list_tools`, never `call_tool`). |
| Deterministic capability resolver and its score | `src/newton_mcp/runtime/resolver.py` | Mock-validated; no LLM, no network call of its own. |
| `policy.yaml` and the policy engine | `src/newton_mcp/action/policy.py` | Mock-validated; deterministic auto/confirm/deny. |
| Context-bound `Approval` | `src/newton_mcp/action/approval.py` | Mock-validated; context-binding, not authentication (see `docs/safety.md`). |
| `ActionState` lifecycle, including `UNKNOWN` | `src/newton_mcp/runtime/lifecycle.py` | Mock-validated state machine; see `docs/action-runtime.md`. |
| JSONL audit trail | `src/newton_mcp/runtime/audit.py` | Mock-validated; append-only, redacted. |
| Executor | `src/newton_mcp/runtime/executor.py` | Mock-validated; no live actuator has been called. |
| Verifier and the retry rule | `src/newton_mcp/runtime/verifier.py`, `runtime/executor.py` (`run_action()`) | Mock-validated; no live actuator has been polled. |
| This gateway's MCP tool mapping | `src/newton_mcp/server.py` | Mock backend by default; the real Newton adapter is implemented against public docs but not validated against a live account (see `README.md`). |

## Part 3: Open questions for Archetype engineers

Each question below is paired with what this project currently assumes in the absence of an
answer, and the module that assumption lives in, so the question is answerable without reading the
whole repository.

1. **Integration model of Newton Agents with external systems.** *Assumption today:* a Newton
   Agent, or a `/query` call with a strict JSON system prompt, emits a Physical Action Contract,
   and everything downstream of that contract is outside Newton entirely
   (`src/newton_mcp/action/propose.py`, `action/prompts.py`). *Question:* is there a documented,
   supported way for an Agent run to hand a structured intent to an external system directly, or is
   polling an Agent run's `results`/`events` the intended integration boundary?
2. **Sink/action connectors in the node registry.** *Assumption today:* none exists in the public
   docs this project could find, so this project built its own allow-list and catalog instead
   (`src/newton_mcp/runtime/config.py`, `runtime/catalog.py`). *Question:* does the Agents API's
   node registry (`api-reference/agents/list-node-registry.md`) model output sinks or action
   connectors, and if so, would an MCP-client node be expressible there instead of in a separate
   runtime like this one?
3. **Confidence and provenance in outputs.** *Assumption today:*
   `PhysicalActionContract.confidence` is model-authored and the policy engine treats it only as a
   floor (`PolicyRule.min_confidence`, `src/newton_mcp/action/policy.py`);
   `Evidence.observation_id` carries a notion of provenance this project invented, and
   `sanitize_response=False` is kept on every `/query` request so `query_id` and timings survive
   for an audit trail (`src/newton_mcp/newton/models.py`). *Question:* do Newton outputs expose a
   first-class confidence or provenance field this project should consume instead of asking a model
   to self-report one in a JSON system prompt?
4. **Support for post-action verification.** *Assumption today:* verification is entirely this
   project's own (`src/newton_mcp/runtime/verifier.py`): it polls a capability's `read_tool` and
   evaluates a structured condition. The Task Verification Agent (`core-concepts/agents/task-
   verification.md`) verifies that work was performed according to a standard operating procedure
   by observing video data; whether it is suitable for verifying a *commanded physical outcome* is
   not confirmed by any public page this project found. *Question:* is the Task Verification Agent,
   or another Agent, intended for that use, and could such a result be fed back into this runtime as
   a new observation rather than this project polling a `read_tool` itself?

## Closing statement

Nothing in this document, and nothing in this repository's code, reverse-engineers a private
Archetype endpoint or circumvents an access control. Every claim in Part 1 traces to a public page
under `docs.archetypeai.app/`. This project is not affiliated with or endorsed by Archetype AI.
