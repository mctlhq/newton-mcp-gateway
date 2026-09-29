# Renders docs/demo.gif. Run from the repository root: `vhs docs/demo.tape`
Output docs/demo.gif
Require uv
Set Shell "bash"
Set FontSize 14
Set Width 1500
Set Height 440
Set Padding 20
Set Theme "GitHub Dark"
Set TypingSpeed 35ms

Hide
Type "export PS1='$ ' && rm -f examples/smart-home/demo-audit.jsonl && uv sync -q --locked && clear" Enter
Sleep 3s
Show

Type "# Mock Newton proposes an action; the runtime resolves, applies policy, executes, then verifies" Enter
Sleep 500ms
Type "uv run python examples/smart-home/demo.py --mock --audit-path examples/smart-home/demo-audit.jsonl" Sleep 400ms Enter
Sleep 6s
Type "clear" Enter
Type "# AC accepts the call but the room never cools: verified failure, one retry (idempotent), then ESCALATED" Enter
Sleep 500ms
Type "uv run python examples/smart-home/demo.py --mock --ac-offline --audit-path examples/smart-home/demo-audit.jsonl --overwrite" Sleep 400ms Enter
Sleep 8s
