#!/usr/bin/env python3
"""Smart-home testbed: one closed-loop demo run.

Walks a single observation through the full Direction A pipeline --
`propose_action` -> `CapabilityCatalog`/`Resolver` -> `Policy` -> `Approval`
-> `Executor`/`Verifier`/`run_action` -- against the in-process `fake_alice`
actuator, prints a step-by-step trace, and appends one JSONL audit line per
accepted lifecycle transition.

Mock-validated only, per `docs/action-runtime.md`: `--mock` (the default)
opens no socket, spawns no subprocess and reads no credential. `--real`
means real Newton, fake actuator -- see `examples/smart-home/README.md` for
why a real Alice server is not drivable yet. Never selected by any test.

    uv run python examples/smart-home/demo.py --mock

Exit codes: 0 SUCCEEDED, 3 ESCALATED, 4 DENIED, 1 unexpected error.
"""

from __future__ import annotations

import argparse
import functools
import os
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

import anyio

# `examples/` is not an importable package (`pyproject.toml` packages only
# `src/newton_mcp`), so `fake_alice` is resolved from this script's own
# directory regardless of how demo.py is loaded (run directly, or loaded by
# `importlib.util.spec_from_file_location` in tests/test_demo.py).
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from fake_alice import RoomState, build_fake_alice, in_process_factory  # noqa: E402

from newton_mcp.action import (  # noqa: E402
    Decision,
    PhysicalActionContract,
    ProposeActionResult,
    create_approval,
    load_policy,
    propose_action,
)
from newton_mcp.config import DEFAULT_TEXT_MODEL, Settings  # noqa: E402
from newton_mcp.newton.api import build_backend  # noqa: E402
from newton_mcp.newton.mock import MockNewtonBackend  # noqa: E402
from newton_mcp.newton.protocol import NewtonBackend  # noqa: E402
from newton_mcp.runtime import (  # noqa: E402
    ActionState,
    CapabilityCatalog,
    Executor,
    JsonlAuditSink,
    Resolver,
    Verifier,
    load_runtime_config,
    new_action_record,
    run_action,
    transition,
)

RUNTIME_YAML_PATH = _THIS_DIR / "runtime.yaml"
POLICY_YAML_PATH = _THIS_DIR / "policy.yaml"
DEFAULT_AUDIT_PATH = _THIS_DIR / "demo-audit.jsonl"

#: The model name is arbitrary in mock mode (MockNewtonBackend ignores it
#: beyond echoing it back); in real mode it selects Newton's text model.
#: Shared with `newton_mcp.config` so there is one source of truth for the id.
PROPOSE_MODEL = DEFAULT_TEXT_MODEL

#: Fixed observation text for the one kitchen-cooling scenario this demo runs.
OBSERVATION_TEXT = "kitchen 29.4 C, occupied"

#: The verifier's clock/sleep are always simulated (see `_SimulatedClock`),
#: so a large poll_interval costs no wall-clock time; the executor's
#: `anyio.fail_after` call timeout is real wall-clock regardless of mode,
#: but every fake_alice call returns instantly.
CALL_TIMEOUT_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 60.0

#: Slack added on top of the worst-case retry/verification span when
#: computing an approval's `expires_at` (design.md "Approval expiry").
APPROVAL_EXPIRY_SLACK_SECONDS = 60.0

_DETERMINISTIC_START = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


class DemoError(RuntimeError):
    """Raised by `run_demo` for a failure that is not a lifecycle terminal state.

    Covers a failed `propose_action` and an empty resolver candidate list --
    both print their detail before this is raised, and `main()` maps it to
    exit code 1.
    """


@dataclass(frozen=True)
class DemoResult:
    """What `run_demo` returns: the terminal state plus everything a test asserts on."""

    terminal_state: ActionState
    #: Calls to the resolved action tool (`candidate.tool_name`) only -- never read polls.
    tool_call_count: int
    #: Calls to the capability's `read_tool` made by the verifier.
    read_poll_count: int
    observation_id: str
    action_id: str
    tool_call_id: str
    verification_id: str
    audit_path: Path
    approval_expires_at: datetime | None = None


class _SimulatedClock:
    """A float clock paired with a `sleep` that advances it -- no real waiting.

    Same technique as `tests/runtime/conftest.py::DeterministicClock`; used
    as the `Verifier`'s injected `clock`/`sleep` seam in `--mock` mode so the
    contract's 600s `verification.timeout_seconds` is consumed in
    milliseconds of wall-clock time instead of ten real minutes.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _make_stepped_now_fn(start: datetime, step_seconds: float = 1.0) -> Callable[[], datetime]:
    """A deterministic `now_fn`: returns `start`, then `start + step`, `start + 2*step`, ..."""
    state = {"current": start}

    def now_fn() -> datetime:
        value = state["current"]
        state["current"] = value + timedelta(seconds=step_seconds)
        return value

    return now_fn


def _default_id_factory():
    counter = iter(range(1, 1_000_000))

    def factory() -> str:
        return f"{next(counter):016x}"

    return factory


def _default_real_backend_factory() -> NewtonBackend:
    """Build the live Newton backend from `ATAI_API_KEY`/`ATAI_API_ENDPOINT` only.

    `--real` changes only the proposal backend (owner amendment): the
    actuator stays the in-process `fake_alice` regardless. Forces
    `NEWTON_BACKEND=api` on a copy of the environment so `Settings.from_env`
    fails loudly naming `ATAI_API_KEY` when it is absent, without reading any
    Alice-specific variable and without touching `os.environ` itself.
    """
    env = dict(os.environ)
    env["NEWTON_BACKEND"] = "api"
    settings = Settings.from_env(env)
    return build_backend(settings)


def _count_calls(log: list[tuple[str, dict[str, Any]]], tool_name: str | None) -> int:
    """How many `call_log` entries are calls to `tool_name` (0 when it is `None`).

    `fake_alice` logs every call -- action tool and read tool alike -- in one
    list, so the demo classifies entries by tool name: the action tool's
    calls are "actuator tool calls", the read tool's are "read polls". The
    two are never added together.
    """
    if tool_name is None:
        return 0
    return sum(1 for name, _args in log if name == tool_name)


def _approved_by_for_auto(policy_version: str, matched_rule_name: str | None, reason: str) -> str:
    """Build the audit label from structured rule metadata, with a reason fallback."""
    rule_label = matched_rule_name if matched_rule_name is not None else reason
    return f"policy:{policy_version}:{rule_label}"


async def run_demo(
    *,
    real: bool = False,
    ac_offline: bool = False,
    audit_path: str | Path | None = None,
    overwrite: bool = False,
    deterministic: bool = False,
    force_desired_temperature_c: int | None = None,
    real_backend_factory: Callable[[], NewtonBackend] | None = None,
    contract_override: PhysicalActionContract | None = None,
    observation_id_override: str | None = None,
    call_log: list[tuple[str, dict[str, Any]]] | None = None,
) -> DemoResult:
    """Run the closed loop once and return a `DemoResult`.

    `contract_override`/`observation_id_override` are a test-only seam: when
    given, the proposal step (and the mock/real backend entirely) is
    skipped and the supplied contract is driven through the same
    catalog/resolver/policy/approval/execute/verify wiring an ordinary run
    uses -- this is how `tests/test_demo.py` exercises the `announce`
    capability, which `MockNewtonBackend` never proposes.
    """
    resolved_audit_path = Path(audit_path) if audit_path is not None else DEFAULT_AUDIT_PATH
    if overwrite and resolved_audit_path.exists():
        resolved_audit_path.unlink()
    sink = JsonlAuditSink(resolved_audit_path)

    id_factory = _default_id_factory() if deterministic else None
    now_fn = _make_stepped_now_fn(_DETERMINISTIC_START) if deterministic else (lambda: datetime.now(timezone.utc))

    # The verifier's clock/sleep are always simulated -- in --mock AND --real
    # alike (design.md: "Catalog, executor and verifier still use
    # in_process_factory(fake_alice) and the simulated clock, exactly as in
    # mock mode"). Only the proposal backend differs between the two modes;
    # the actuator and its verification loop are identical either way, so
    # the contract's 600s verification.timeout_seconds never costs real
    # wall-clock time regardless of --real/--mock.
    sim_clock = _SimulatedClock()
    verifier_clock: Callable[[], float] = sim_clock.clock
    verifier_sleep: Callable[[float], Any] = sim_clock.sleep

    room_state = RoomState(
        room="kitchen",
        temperature_c=29.4,
        occupancy=True,
        ac_online=not ac_offline,
        ac_target_c=None,
        light_on=False,
        brightness_pct=50,
        cooling_step_c=2.0,
    )
    log: list[tuple[str, dict[str, Any]]] = call_log if call_log is not None else []
    fake_server = build_fake_alice(room_state, call_log=log)
    client_factory = in_process_factory(fake_server)

    print("actuator: in-process fake (fake_alice), not a real device")

    runtime_config = load_runtime_config(RUNTIME_YAML_PATH)

    if contract_override is not None:
        contract = contract_override
        observation_id = observation_id_override or f"obs-{secrets.token_hex(8)}"
        print(f"contract: goal={contract.goal!r} reason={contract.reason!r} (test-supplied, no propose step)")
    else:
        if real:
            backend: NewtonBackend = (real_backend_factory or _default_real_backend_factory)()
            print("proposal: live Newton backend; actuator: in-process fake")
        else:
            backend = MockNewtonBackend()

        allowed_goals = sorted(
            {prefix for capability in runtime_config.capabilities for prefix in capability.goal_prefixes}
        )

        propose_result: ProposeActionResult = await propose_action(
            backend,
            model=PROPOSE_MODEL,
            text_events=[OBSERVATION_TEXT],
            allowed_goals=allowed_goals,
            observation_id=observation_id_override,
        )
        print(f"propose: backend={propose_result.backend!r} status={propose_result.status!r}")

        if propose_result.status != "completed" or propose_result.contract is None:
            for error in propose_result.errors:
                print(f"  propose error[attempt={error.attempt}] {error.kind}: {error.message}")
            raise DemoError("propose_action did not return a validated contract; see the printed errors above")

        contract = propose_result.contract
        observation_id = propose_result.observation_id
        print(f"contract: goal={contract.goal!r} reason={contract.reason!r}")

    if force_desired_temperature_c is not None:
        contract = contract.model_copy(
            update={"constraints": {**contract.constraints, "desired_temperature_c": force_desired_temperature_c}}
        )
        print(
            "testbed override (not Newton output): constraints.desired_temperature_c forced to "
            f"{force_desired_temperature_c}"
        )

    start_now = now_fn()
    record = new_action_record(now=start_now, observation_id=observation_id, id_factory=id_factory)

    catalog = CapabilityCatalog(runtime_config, client_factory=client_factory)
    snapshot = await catalog.refresh()
    for problem in snapshot.problems:
        print(f"catalog problem: kind={problem.kind} server={problem.server} tool={problem.tool} detail={problem.detail}")

    resolution = Resolver(catalog).resolve(contract)
    for candidate in resolution.candidates:
        print(f"candidate: tool={candidate.tool_name} score={candidate.score:.2f} why={candidate.why}")
    for rejection in resolution.rejections:
        print(f"rejected: tool={rejection.tool_name} stage={rejection.stage} detail={rejection.detail}")

    if not resolution.candidates:
        raise DemoError("resolver returned no candidates; see the printed rejections above")

    candidate = resolution.candidates[0]

    policy = load_policy(POLICY_YAML_PATH)
    decision = policy.evaluate(contract, candidate)
    print(f"policy: decision={decision.decision.value} reason={decision.reason!r}")

    approval_span_seconds = (
        (contract.verification.retry_limit + 1) * (CALL_TIMEOUT_SECONDS + contract.verification.timeout_seconds)
        + APPROVAL_EXPIRY_SLACK_SECONDS
    )
    expires_at = start_now + timedelta(seconds=approval_span_seconds)

    if decision.decision is Decision.DENY:
        record = transition(
            record, ActionState.DENIED, decision.reason, now=start_now, sink=sink, id_factory=id_factory
        )
        print(f"DENIED: {decision.reason}")
        return DemoResult(
            terminal_state=ActionState.DENIED,
            tool_call_count=_count_calls(log, candidate.tool_name),
            read_poll_count=_count_calls(log, candidate.read_tool),
            observation_id=record.observation_id,
            action_id=record.action_id,
            tool_call_id=record.tool_call_id,
            verification_id=record.verification_id,
            audit_path=resolved_audit_path,
        )

    if decision.decision is Decision.CONFIRM:
        if real:
            answer = input(f"Confirm {candidate.tool_name}({candidate.args})? [y/N] ")
            confirmed = answer.strip().lower() in ("y", "yes")
            approved_by = "operator:stdin-confirm"
        else:
            confirmed = True
            approved_by = f"mock:auto-confirm:{policy.policy_version}"
        if not confirmed:
            record = transition(
                record, ActionState.DENIED, "operator declined confirmation", now=start_now, sink=sink,
                id_factory=id_factory,
            )
            print("DENIED: operator declined confirmation")
            return DemoResult(
                terminal_state=ActionState.DENIED,
                tool_call_count=_count_calls(log, candidate.tool_name),
                read_poll_count=_count_calls(log, candidate.read_tool),
                observation_id=record.observation_id,
                action_id=record.action_id,
                tool_call_id=record.tool_call_id,
                verification_id=record.verification_id,
                audit_path=resolved_audit_path,
            )
    else:
        approved_by = _approved_by_for_auto(
            policy.policy_version, decision.matched_rule_name, decision.reason
        )

    approval = create_approval(
        candidate,
        action_id=record.action_id,
        policy_version=policy.policy_version,
        approved_by=approved_by,
        approved_at=start_now,
        expires_at=expires_at,
        approval_id=f"appr-{(id_factory or (lambda: secrets.token_hex(8)))()}",
    )
    record = transition(
        record, ActionState.AUTHORIZED, f"approved by {approved_by!r}", now=start_now, sink=sink,
        id_factory=id_factory,
    )

    executor = Executor(catalog, client_factory=client_factory, call_timeout_seconds=CALL_TIMEOUT_SECONDS, sink=sink, id_factory=id_factory)
    verifier = Verifier(
        catalog,
        client_factory=client_factory,
        poll_interval_seconds=POLL_INTERVAL_SECONDS,
        sink=sink,
        id_factory=id_factory,
        clock=verifier_clock,
        sleep=verifier_sleep,
    )

    record, terminal_state = await run_action(
        candidate,
        contract,
        record,
        approval=approval,
        policy_version=policy.policy_version,
        executor=executor,
        verifier=verifier,
        now_fn=now_fn,
    )

    tool_call_count = _count_calls(log, candidate.tool_name)
    read_poll_count = _count_calls(log, candidate.read_tool)
    print(f"terminal state: {terminal_state.name}")
    print(f"actuator tool calls: {tool_call_count}")
    print(f"read polls: {read_poll_count}")
    print(
        f"correlation ids: observation_id={record.observation_id} action_id={record.action_id} "
        f"tool_call_id={record.tool_call_id} verification_id={record.verification_id}"
    )
    print(f"audit path: {resolved_audit_path}")

    return DemoResult(
        terminal_state=terminal_state,
        tool_call_count=tool_call_count,
        read_poll_count=read_poll_count,
        observation_id=record.observation_id,
        action_id=record.action_id,
        tool_call_id=record.tool_call_id,
        verification_id=record.verification_id,
        audit_path=resolved_audit_path,
        approval_expires_at=expires_at,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="demo.py",
        description=(
            "Smart-home closed-loop demo: observation -> propose_action -> resolve -> "
            "policy -> approve -> execute -> verify, against the in-process fake_alice actuator."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--mock", action="store_true", help="Use MockNewtonBackend for the proposal step (default)."
    )
    mode.add_argument(
        "--real",
        action="store_true",
        help=(
            "Use the live Newton backend (ATAI_API_KEY/ATAI_API_ENDPOINT) for the proposal step only; "
            "the actuator stays the in-process fake_alice, never a real device."
        ),
    )
    parser.add_argument(
        "--ac-offline",
        action="store_true",
        help="Simulate an AC that accepts set_ac_temperature but never actually cools the room.",
    )
    parser.add_argument(
        "--audit-path",
        type=Path,
        default=None,
        help=f"Path for the JSONL audit trail (default: {DEFAULT_AUDIT_PATH}).",
    )
    parser.add_argument("--overwrite", action="store_true", help="Truncate --audit-path before the run.")
    parser.add_argument(
        "--deterministic",
        action="store_true",
        help="Use a counter-based id_factory and a fixed, stepped clock (used to record trace.jsonl).",
    )
    parser.add_argument(
        "--force-desired-temperature-c",
        type=int,
        default=None,
        help=(
            "Testbed override: rewrite the proposed contract's constraints.desired_temperature_c "
            "after propose_action, not Newton's output."
        ),
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return _build_parser().parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        result = anyio.run(
            functools.partial(
                run_demo,
                real=args.real,
                ac_offline=args.ac_offline,
                audit_path=args.audit_path,
                overwrite=args.overwrite,
                deterministic=args.deterministic,
                force_desired_temperature_c=args.force_desired_temperature_c,
            )
        )
    except (DemoError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if result.terminal_state is ActionState.SUCCEEDED:
        return 0
    if result.terminal_state is ActionState.ESCALATED:
        return 3
    if result.terminal_state is ActionState.DENIED:
        return 4
    return 1  # pragma: no cover - unreachable: run_action/transition only reach the states above


if __name__ == "__main__":
    raise SystemExit(main())
