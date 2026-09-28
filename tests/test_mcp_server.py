async def test_lists_exactly_the_documented_tools(server):
    names = {t.name for t in await server.list_tools()}
    assert names == {"newton_query", "newton_embed_timeseries"}


async def test_tools_are_marked_read_only(server):
    for t in await server.list_tools():
        assert t.annotations and t.annotations.read_only_hint is True
