"""Docs-consistency tests: check the doc corpus against the real repo, not manually.

Standard library only, no network. Corpus: `README.md`, `AGENTS.md`,
`CONTRIBUTING.md`, every `docs/*.md`, and `examples/smart-home/README.md`.
Four checks, one test function each -- see requirements.md's owner amendment
(2026-09-29) and design.md section 5 for what each must do and why:

1. Every relative markdown link and backticked repo path resolves.
2. Every `--flag` in a command invoking `examples/smart-home/demo.py` is a
   real option of the demo's own `_build_parser()`.
3. Every `uv run <name>` target is a real `[project.scripts]` key, a real
   `[tool.<name>]` table in `pyproject.toml`, or `python` followed by
   `-c`/`-m` or an existing script path.
4. Every `NEWTON_*`/`ATAI_*` token in the corpus is actually read by the code
   (ast-collected) or assigned in `.env.example`.

This module touches no package code and adds no entry to `pyproject.toml`
(`testpaths = ["tests"]` already covers it).
"""

from __future__ import annotations

import ast
import importlib.util
import re
import shlex
import sys
import tomllib
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent

CORPUS_RELATIVE_PATHS: list[str] = [
    "README.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    *sorted(p.relative_to(REPO_ROOT).as_posix() for p in (REPO_ROOT / "docs").glob("*.md")),
    "examples/smart-home/README.md",
]


def _corpus() -> list[tuple[str, Path, str]]:
    """Return `(relative_path, absolute_path, text)` for every corpus file."""
    out: list[tuple[str, Path, str]] = []
    for rel in CORPUS_RELATIVE_PATHS:
        path = REPO_ROOT / rel
        out.append((rel, path, path.read_text(encoding="utf-8")))
    return out


def _gitignored_paths() -> set[str]:
    """Literal relative paths named in `.gitignore` (comments and blanks skipped).

    A path a doc names as a default *output* location and explicitly calls
    "gitignored" (e.g. `examples/smart-home/demo-audit.jsonl`) never exists in
    a fresh checkout; checking it against `.gitignore` rather than the
    filesystem still verifies something concrete (the path is a recognized
    generated artefact, not a typo) without weakening the check for a genuine
    dead link.
    """
    gitignore = REPO_ROOT / ".gitignore"
    if not gitignore.is_file():
        return set()
    paths = set()
    for line in gitignore.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        paths.add(stripped.rstrip("/"))
    return paths


# ---------------------------------------------------------------------------
# Shared: extracting "commands" from fenced code blocks and inline code spans
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_INLINE_RE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")


def _has_open_quote(text: str) -> bool:
    """True when `text` ends inside an unclosed shell quote."""
    try:
        shlex.split(text)
    except ValueError as exc:
        return "No closing quotation" in str(exc)
    return False


def _join_continuations(block: str) -> list[str]:
    """Join a fenced code block's physical lines into logical commands, as a shell would.

    A line ending in `\\` continues onto the next, and so does a line that
    leaves a quote open -- a multi-line `python -c "..."` is one command. A
    command still unterminated at the end of the block is returned as-is, so
    the caller's tokeniser reports it rather than it vanishing.
    """
    logical_lines: list[str] = []
    buffer = ""
    in_quote = False
    for raw_line in block.splitlines():
        if in_quote:
            combined = f"{buffer}\n{raw_line}"
        else:
            stripped = raw_line.strip()
            combined = f"{buffer} {stripped}".strip() if buffer else stripped
            if not buffer and combined.startswith("#"):
                continue
        in_quote = _has_open_quote(combined)
        if in_quote:
            buffer = combined
            continue
        if combined.endswith("\\"):
            buffer = combined[:-1].rstrip()
            continue
        buffer = ""
        if combined:
            logical_lines.append(combined)
    if buffer:
        logical_lines.append(buffer)
    return logical_lines


def _extract_commands(text: str) -> list[str]:
    """Every logical command line from fenced code blocks, plus every inline code span."""
    commands: list[str] = []
    spans: list[tuple[int, int]] = []
    for match in _FENCE_RE.finditer(text):
        spans.append((match.start(), match.end()))
        commands.extend(_join_continuations(match.group(1)))

    remainder_parts = []
    cursor = 0
    for start, end in spans:
        remainder_parts.append(text[cursor:start])
        cursor = end
    remainder_parts.append(text[cursor:])
    remainder = "".join(remainder_parts)

    for match in _INLINE_RE.finditer(remainder):
        span = match.group(1).strip()
        if span:
            commands.append(span)
    return commands


# ---------------------------------------------------------------------------
# T1: relative links and backticked repo paths resolve
# ---------------------------------------------------------------------------

_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_BACKTICK_PATH_RE = re.compile(r"`((?:docs|examples|src|tests)/[^`\s)]+)`")


def _strip_anchor(target: str) -> str:
    return target.split("#", 1)[0].strip()


def test_relative_links_resolve() -> None:
    ignored = _gitignored_paths()
    failures: list[str] = []

    for rel, path, text in _corpus():
        for match in _MD_LINK_RE.finditer(text):
            target = _strip_anchor(match.group(1))
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            resolved = (path.parent / target).resolve()
            if not resolved.exists():
                failures.append(f"{rel}: link target {target!r} does not resolve to {resolved}")

        for match in _BACKTICK_PATH_RE.finditer(text):
            target = _strip_anchor(match.group(1))
            if target in ignored:
                continue
            resolved = (REPO_ROOT / target).resolve()
            if not resolved.exists():
                failures.append(f"{rel}: backticked path {target!r} does not resolve to {resolved}")

    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# T2: demo.py commands use real flags
# ---------------------------------------------------------------------------

_DEMO_PATH = REPO_ROOT / "examples" / "smart-home" / "demo.py"


def _load_demo_module() -> ModuleType:
    """Load `demo.py` by path, exactly as `tests/test_demo.py` does.

    `examples/` is not an importable package, so `spec_from_file_location` is
    the only way to reach `_build_parser()`. A distinct module name keeps
    this load independent of `tests/test_demo.py`'s own fixture, which loads
    the same file under a different name.
    """
    spec = importlib.util.spec_from_file_location("_docs_consistency_demo_under_test", _DEMO_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _demo_parser_option_strings() -> set[str]:
    module = _load_demo_module()
    parser = module._build_parser()
    options: set[str] = set()
    for action in parser._actions:
        options.update(action.option_strings)
    return options


def _looks_like_demo_command(command: str) -> bool:
    return "examples/smart-home/demo.py" in command or command.strip().startswith("demo.py")


def _demo_flag_failures(docs: list[tuple[str, str]], valid_options: set[str]) -> list[str]:
    """Failures for every demo.py command in `docs` (`(relative_path, text)` pairs).

    A command that cannot be tokenised is itself a failure: an unchecked
    command is not a checked-and-clean one.
    """
    failures: list[str] = []
    for rel, text in docs:
        for command in _extract_commands(text):
            if not _looks_like_demo_command(command):
                continue
            try:
                tokens = shlex.split(command)
            except ValueError as exc:
                failures.append(f"{rel}: could not tokenise demo.py command {command!r}: {exc}")
                continue
            for token in tokens:
                if not token.startswith("--"):
                    continue
                flag = token.split("=", 1)[0]
                if flag not in valid_options:
                    failures.append(f"{rel}: {flag!r} in {command!r} is not a demo.py option")
    return failures


def test_demo_commands_use_real_flags() -> None:
    docs = [(rel, text) for rel, _path, text in _corpus()]
    failures = _demo_flag_failures(docs, _demo_parser_option_strings())
    assert not failures, "\n".join(failures)


def test_untokenisable_demo_command_is_a_failure_not_a_skip() -> None:
    doc = "```bash\nuv run python examples/smart-home/demo.py --mock 'unclosed\n```\n"
    failures = _demo_flag_failures([("synthetic.md", doc)], _demo_parser_option_strings())
    assert len(failures) == 1 and "could not tokenise" in failures[0]


# ---------------------------------------------------------------------------
# T3: `uv run <name>` targets exist
# ---------------------------------------------------------------------------


def _pyproject() -> dict:
    with open(REPO_ROOT / "pyproject.toml", "rb") as handle:
        return tomllib.load(handle)


def _project_script_names(pyproject: dict) -> set[str]:
    return set(pyproject.get("project", {}).get("scripts", {}).keys())


def _tool_table_names(pyproject: dict) -> set[str]:
    return set(pyproject.get("tool", {}).keys())


def _uv_run_invocations(command: str) -> list[tuple[str, str | None]]:
    """Every `(name, next_token_or_None)` for `uv run <name> [next ...]` in `command`.

    Leading `VAR=value` assignments before `uv` are irrelevant to this scan --
    the token sequence is matched wherever `uv run` appears, regardless of
    what precedes it.
    """
    tokens = shlex.split(command)  # ValueError propagates: the caller reports it
    results: list[tuple[str, str | None]] = []
    for i in range(len(tokens) - 2):
        if tokens[i] == "uv" and tokens[i + 1] == "run":
            name = tokens[i + 2]
            following = tokens[i + 3] if i + 3 < len(tokens) else None
            results.append((name, following))
    return results


def _uv_run_failures(docs: list[tuple[str, str]], pyproject: dict) -> list[str]:
    """Failures for every `uv run` command in `docs` (`(relative_path, text)` pairs).

    A command that cannot be tokenised is itself a failure, as in T2.
    """
    script_names = _project_script_names(pyproject)
    tool_names = _tool_table_names(pyproject)
    failures: list[str] = []

    for rel, text in docs:
        for command in _extract_commands(text):
            if "uv run" not in command:
                continue
            try:
                invocations = _uv_run_invocations(command)
            except ValueError as exc:
                failures.append(f"{rel}: could not tokenise uv run command {command!r}: {exc}")
                continue
            for name, following in invocations:
                if name in script_names or name in tool_names:
                    continue
                if name == "python":
                    if following in ("-c", "-m"):
                        continue
                    if following is not None and (REPO_ROOT / following).resolve().is_file():
                        continue
                    failures.append(
                        f"{rel}: 'uv run python {following}' in {command!r} is not -c/-m or an "
                        "existing script path"
                    )
                    continue
                failures.append(
                    f"{rel}: 'uv run {name}' in {command!r} is not a [project.scripts] key or a "
                    "[tool.<name>] table in pyproject.toml"
                )
    return failures


def test_uv_run_targets_exist() -> None:
    docs = [(rel, text) for rel, _path, text in _corpus()]
    failures = _uv_run_failures(docs, _pyproject())
    assert not failures, "\n".join(failures)


def test_untokenisable_uv_run_command_is_a_failure_not_a_skip() -> None:
    doc = "```bash\nuv run newton-mcp 'unclosed\n```\n"
    failures = _uv_run_failures([("synthetic.md", doc)], _pyproject())
    assert len(failures) == 1 and "could not tokenise" in failures[0]


# ---------------------------------------------------------------------------
# T4: NEWTON_*/ATAI_* names are read by the code, or assigned in .env.example
# ---------------------------------------------------------------------------

_ENV_TOKEN_RE = re.compile(r"\b(?:NEWTON|ATAI)_[A-Z0-9_]+\b")
_ENV_ASSIGNMENT_RE = re.compile(r"^#?\s*((?:NEWTON|ATAI)_[A-Z0-9_]+)=", re.MULTILINE)


def _string_tuple_elements(node: ast.AST | None, bindings: dict[str, ast.AST]) -> list[str]:
    """Resolve `node` to a tuple/list of string literals, following one level of name binding."""
    if node is None:
        return []
    if isinstance(node, ast.Name) and node.id in bindings:
        return _string_tuple_elements(bindings[node.id], bindings)
    if isinstance(node, (ast.Tuple, ast.List)):
        return [elt.value for elt in node.elts if isinstance(elt, ast.Constant) and isinstance(elt.value, str)]
    return []


def _is_os_environ(node: ast.AST) -> bool:
    """`os.environ`, or a bare `environ` imported from `os`."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    ) or (isinstance(node, ast.Name) and node.id == "environ")


def _is_env_lookup_call(func: ast.AST) -> bool:
    """`env.get`, `os.environ.get` / `environ.get`, or `os.getenv` / `getenv`.

    `env` is the name `newton_mcp.config` gives its injected environment
    mapping. Any other `.get(...)` -- a response body, a dict of ids -- is not
    an environment lookup and does not count.
    """
    if isinstance(func, ast.Attribute) and func.attr == "get":
        receiver = func.value
        return (isinstance(receiver, ast.Name) and receiver.id == "env") or _is_os_environ(receiver)
    if isinstance(func, ast.Attribute) and func.attr == "getenv":
        return isinstance(func.value, ast.Name) and func.value.id == "os"
    return isinstance(func, ast.Name) and func.id == "getenv"


def _collect_env_names_from_file(path: Path) -> set[str]:
    return _collect_env_names_from_source(path.read_text(encoding="utf-8"), str(path))


def _collect_env_names_from_source(source: str, filename: str = "<source>") -> set[str]:
    tree = ast.parse(source, filename=filename)
    names: set[str] = set()

    # Module-level (and any-scope) simple name bindings, for resolving a Name
    # argument back to its tuple literal (e.g. `_resolve(env, HOST_VARS, ...)`
    # where `HOST_VARS = ("NEWTON_MCP_HOST", "HOST")`).
    bindings: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            bindings[node.targets[0].id] = node.value
        # Module-level constants ending in `_ENV_VAR`.
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id.endswith("_ENV_VAR")
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            names.add(node.value.value)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            # `env.get("NAME")`, `os.environ.get("NAME")`, `os.getenv("NAME")`.
            if (
                _is_env_lookup_call(node.func)
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                names.add(node.args[0].value)

            # `_resolve(env, <names-tuple>, default)`.
            func_name = node.func.id if isinstance(node.func, ast.Name) else (
                node.func.attr if isinstance(node.func, ast.Attribute) else None
            )
            if func_name == "_resolve":
                for arg in node.args:
                    names.update(_string_tuple_elements(arg, bindings))

        # `os.environ["NAME"]` (or `environ["NAME"]` if imported directly).
        if isinstance(node, ast.Subscript):
            if _is_os_environ(node.value) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                names.add(node.slice.value)

    return names


def _code_read_env_names() -> set[str]:
    names: set[str] = set()
    for path in sorted((REPO_ROOT / "src" / "newton_mcp").rglob("*.py")):
        names |= _collect_env_names_from_file(path)
    return names


def _env_example_names() -> set[str]:
    env_example = REPO_ROOT / ".env.example"
    if not env_example.is_file():
        return set()
    text = env_example.read_text(encoding="utf-8")
    return set(_ENV_ASSIGNMENT_RE.findall(text))


def test_env_vars_are_read_by_code() -> None:
    allowed = _code_read_env_names() | _env_example_names()
    failures: list[str] = []

    for rel, _path, text in _corpus():
        for token in _ENV_TOKEN_RE.findall(text):
            if token not in allowed:
                failures.append(f"{rel}: {token!r} is not read by src/newton_mcp/**/*.py or assigned in .env.example")

    assert not failures, "\n".join(sorted(set(failures)))


def test_only_real_env_access_patterns_count() -> None:
    source = """
import os
from os import environ, getenv
a = env.get("NEWTON_VIA_ENV_GET")
b = os.environ.get("NEWTON_VIA_OS_ENVIRON_GET")
c = os.getenv("NEWTON_VIA_OS_GETENV")
d = os.environ["NEWTON_VIA_SUBSCRIPT"]
e = environ.get("NEWTON_VIA_ENVIRON_GET")
f = getenv("NEWTON_VIA_GETENV")
g = body.get("NEWTON_VIA_RESPONSE_BODY")
h = {}.get("NEWTON_VIA_DICT_LITERAL")
i = ids.get("NEWTON_VIA_IDS")
"""
    assert _collect_env_names_from_source(source) == {
        "NEWTON_VIA_ENV_GET",
        "NEWTON_VIA_OS_ENVIRON_GET",
        "NEWTON_VIA_OS_GETENV",
        "NEWTON_VIA_SUBSCRIPT",
        "NEWTON_VIA_ENVIRON_GET",
        "NEWTON_VIA_GETENV",
    }


def test_quoted_multiline_command_is_one_command() -> None:
    block = 'uv run python -c "\nimport json\n# a comment inside the string\n" > out.json\nuv run pytest\n'
    commands = _join_continuations(block)
    assert commands == ['uv run python -c "\nimport json\n# a comment inside the string\n" > out.json', "uv run pytest"]
