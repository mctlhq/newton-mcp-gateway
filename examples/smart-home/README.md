# Smart-home testbed: an end-to-end closed-loop demo

> **Mock-validated only.** Every result on this page comes from `--mock`: `MockNewtonBackend`
> plus the in-process fake actuator `fake_alice.py`. No code here has run against a live Newton
> account or a real smart-home server. The Physical Action Contract and the action runtime remain
> **this project's experimental proposal**, not an Archetype standard or an Archetype-confirmed
> capability -- see the repository root `README.md` and `docs/action-runtime.md`.

This directory is a self-contained artefact a reviewer can *run*: it walks one observation
("kitchen 29.4 C, occupied") through every stage of the Direction A pipeline --
`propose_action` → capability resolution → policy → approval → execution → verification -- against
a simulated kitchen, prints a step-by-step trace, and appends a JSONL audit line per accepted
lifecycle transition. Nothing under `src/newton_mcp/` changes for this: the runtime stays
actuator-agnostic, and everything actuator-specific (the fake server, its tool shapes, its room
simulation) lives only here.

"Alice" in this testbed is a stand-in for an **open-source smart-home MCP server**: a plausible
tool shape chosen because it is real hardware with benign, reversible actions. It is not a
specific hosted deployment, and none is linked from this page.

## Capabilities

| Tool | Goal prefixes | Idempotent | `read_tool` |
|---|---|---|---|
| `set_ac_temperature` | `reduce_room_temperature`, `raise_room_temperature` | `true` | `get_room_state` |
| `set_light_state` | `turn_on_light`, `turn_off_light` | `true` | `get_room_state` |
| `set_light_brightness` | `set_light_brightness` | `true` | `get_room_state` |
| `announce` | `announce` | `false` | *(none -- unverifiable by design)* |

`examples/smart-home/runtime.yaml` declares these against one server, `alice`
(`streamable-http`, a deliberately non-resolvable RFC 2606 placeholder URL
`https://alice.invalid/mcp`). `examples/smart-home/policy.yaml` (`policy_version:
"smart-home.v1"`, `default: deny`) auto-approves the AC and lighting capabilities within safe
bounds -- `target_temperature_c` is bound to an inclusive **20-25 C** band, `brightness_pct` to
**0-100** -- and requires confirmation for `announce`. No lock, oven, alarm, industrial start/stop
or safety-system tool appears anywhere in either file, per the epic's hard rule.

`fake_alice.py`'s own schema for `set_ac_temperature` accepts any integer: the 20-25 C band is
enforced by `policy.yaml` alone, never by the actuator's schema, so a denial in the demo is
provably the policy's.

**Transport vs. binding.** The in-process client factory (`fake_alice.in_process_factory`)
ignores `runtime.yaml`'s declared `streamable-http` transport entirely -- it always connects
in-process, no socket. `ServerConfig.binding_identity` (and so the `Approval` binding) is still
computed from the *declared* transport, never from how the fake actually connects.

## Running it

```bash
uv run python examples/smart-home/demo.py --mock
```

### The success path

The kitchen starts at 29.4 C, occupied. `MockNewtonBackend` proposes the repo's example contract
(goal `reduce_room_temperature`, target `23 C`, verification `temperature_c <= 24`). The resolver
picks `set_ac_temperature`; the policy auto-approves it (23 is within 20-25); the executor calls
it; the verifier polls `get_room_state` and watches the simulated room cool one step per poll
until the condition is satisfied. Excerpt (mock, `--deterministic`):

```
propose: backend='mock' status='completed'
contract: goal='reduce_room_temperature' reason='[mock] The occupied kitchen reached 29.4 C while the previous window was unoccupied.'
candidate: tool=set_ac_temperature score=1.00 why=goal prefix 'reduce_room_temperature' matched; explicit location match; read tool discovered (score=1.00)
policy: decision=auto reason="matched rule 'ac-reduce-temperature'"
terminal state: SUCCEEDED
actuator tool calls: 1
read polls: 3
```

### The failure path: AC offline

```bash
uv run python examples/smart-home/demo.py --mock --ac-offline
```

`fake_alice`'s `set_ac_temperature` still answers `{"accepted": true}` -- a digitally successful
call -- but the simulated room's temperature never moves while the AC is offline. The verifier
obtains real observations (never zero), never satisfies the condition, and the run retries once
(the mock contract's `verification.retry_limit` is `1`) before escalating:

```
terminal state: ESCALATED
actuator tool calls: 2
read polls: 20
```

`actuator tool calls` counts only calls to the action tool (`set_ac_temperature`): the first
attempt plus the one retry. `read polls` counts the verifier's `get_room_state` calls; the two are
never added together. Exit code `3` (`ESCALATED`).

This demonstrates the exact claim `docs/action-runtime.md` makes: a successful MCP response is
not a successful physical action, and this runtime does not confuse the two.

### The non-idempotent path: `announce`

`announce` has no `read_tool` -- its outcome can never be verified -- so any run through it always
ends `ESCALATED` after **exactly one** call, never retried. `tests/test_demo.py` exercises this
capability directly (it is not a second CLI scenario; see "Out of scope" in the proposal).

## Flags

| Flag | Effect |
|---|---|
| `--mock` | Use `MockNewtonBackend` for the proposal step (default). |
| `--real` | Use the live Newton backend for the proposal step only -- see "Real mode" below. |
| `--ac-offline` | Simulate an AC that accepts the call but never cools the room. |
| `--audit-path PATH` | JSONL audit file (default `examples/smart-home/demo-audit.jsonl`, gitignored). |
| `--overwrite` | Truncate `--audit-path` before the run (the sink itself only ever appends). |
| `--deterministic` | Counter-based `id_factory` + a fixed, stepped clock. Used to record `trace.jsonl`. |
| `--force-desired-temperature-c N` | **Testbed override**, applied *after* `propose_action` -- rewrites the proposed contract's `constraints.desired_temperature_c`. This is never Newton's output; it exists to demonstrate and test the policy denial deterministically. |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | `ActionState.SUCCEEDED` |
| `3` | `ActionState.ESCALATED` |
| `4` | `ActionState.DENIED` |
| `1` | Unexpected error (a failed proposal, an empty candidate list, a missing `--real` credential) |

## `trace.jsonl`

The committed `examples/smart-home/trace.jsonl` is a recorded mock **success** run: five lines,
`PROPOSED -> AUTHORIZED -> EXECUTING -> EXECUTED -> VERIFYING -> SUCCEEDED`, each carrying all
four non-empty correlation ids. Regenerate it with:

```bash
uv run python examples/smart-home/demo.py --mock --deterministic \
  --audit-path examples/smart-home/trace.jsonl --overwrite
```

The AC-offline excerpt above comes from the demo's printed stdout trace, not from a second
committed file -- appending two runs into one JSONL file would mix two unrelated `action_id`s in
one artefact.

## Real mode: real Newton, fake actuator

`--real` changes **only the proposal step**. It reads Newton credentials from `ATAI_API_KEY` /
`ATAI_API_ENDPOINT` only (never from a committed file or a CLI argument) and fails loudly, naming
the missing variable, when either is absent. Execution and verification always run against the
in-process `fake_alice` -- there is no `ALICE_MCP_URL` and no path to a real Alice server in this
testbed. The printed trace says so explicitly: "proposal: live Newton backend; actuator:
in-process fake".

**Why a real Alice server is not drivable yet.** The real, public server (`mctlhq/mctl-alice`) was
checked at owner review:

- Its `alice_get_device_state` tool returns Markdown **text only** -- no `structuredContent` --
  so every poll through this runtime's verifier would be "no observation" and every run would end
  `ESCALATED`, never a genuine verification.
- It sits behind **OAuth**. `HttpTransport` in `runtime/config.py` now supports a static bearer
  (or other single-header) credential resolved from an environment variable (mock-validated only;
  see `docs/action-runtime.md`'s "Authenticated streamable-http"), but Alice's own OAuth
  authorization-code flow is a separate, larger piece of work this does not attempt
  (`mctlhq/newton-mcp-gateway#28`).
- It addresses devices by **`device` id**, not by room, which does not match this testbed's
  `target.location`-based capability shape.

Those gaps are tracked in `mctlhq/mctl-alice#47` (structured read-only state) and
`mctlhq/newton-mcp-gateway#28` (authenticated streamable-http). `--real` here means real Newton
with the fake actuator, is opt-in, and is never selected by any test in this repository.

## What this is not

This demo does not prove a live Newton integration, does not prove a live actuator integration,
and does not change any behaviour in `src/newton_mcp/`. Every result is mock-validated, and the
Physical Action Contract plus the action runtime remain this project's experimental proposal.
