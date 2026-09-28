from newton_mcp import server as server_module


async def test_lists_exactly_the_documented_tools(server):
    names = {t.name for t in await server.list_tools()}
    assert names == {"newton_query", "newton_embed_timeseries", "newton_analyze_image"}


async def test_tools_are_marked_read_only(server):
    for t in await server.list_tools():
        assert t.annotations and t.annotations.read_only_hint is True


async def test_analyze_image_schema_has_no_upload_property(server):
    tools = {t.name: t for t in await server.list_tools()}
    schema = tools["newton_analyze_image"].input_schema
    assert "upload" not in schema.get("properties", {})


def test_main_dispatches_stdio_by_default(monkeypatch):
    calls = []

    def fake_run(self, transport, **kwargs):
        calls.append((transport, kwargs))

    monkeypatch.setattr(server_module.MCPServer, "run", fake_run)
    monkeypatch.setenv("NEWTON_BACKEND", "mock")
    monkeypatch.delenv("NEWTON_MCP_TRANSPORT", raising=False)
    server_module.main()
    assert calls == [("stdio", {})]


def test_main_dispatches_streamable_http_with_host_and_port(monkeypatch):
    calls = []

    def fake_run(self, transport, **kwargs):
        calls.append((transport, kwargs))

    monkeypatch.setattr(server_module.MCPServer, "run", fake_run)
    monkeypatch.setenv("NEWTON_BACKEND", "mock")
    monkeypatch.setenv("NEWTON_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("PORT", "9100")
    server_module.main()
    assert calls == [("streamable-http", {"host": "0.0.0.0", "port": 9100})]
