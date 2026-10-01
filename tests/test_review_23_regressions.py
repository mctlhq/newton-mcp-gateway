"""Regressions found when verifying the merged small-follow-up scope."""

import asyncio

import pytest
from mcp import Client
from pydantic import ValidationError
from newton_mcp.action.propose import propose_action
from newton_mcp.config import Settings
from newton_mcp.server import _surface_input_errors, create_server
from newton_mcp.newton.mock import MockNewtonBackend
from conftest import ScriptedNewtonBackend

@pytest.mark.parametrize('outputs', [[], [42]])
async def test_final_attempt_without_text_clears_previous_text(outputs):
    backend = ScriptedNewtonBackend([
        {'status': 'completed', 'outputs': ['earlier invalid text']},
        {'status': 'completed', 'outputs': outputs},
    ])
    result = await propose_action(backend, model='Newton::test', text_events=['hot'])
    assert result.status == 'failed'
    assert result.raw_text is None

@pytest.mark.parametrize('arguments', [
    {'question': 'q', 'file_id': 'private-sentinel', 'image_base64': 'a', 'mime_type': 'image/png'},
    {'question': 'q', 'file_id': 'private-sentinel'},
])
async def test_input_error_does_not_echo_file_id(arguments):
    backend = MockNewtonBackend()
    async with Client(create_server(Settings(), backend=backend)) as client:
        result = await client.call_tool('newton_analyze_image', arguments)
    assert result.is_error
    assert 'private-sentinel' not in ' '.join(getattr(c, 'text', '') for c in result.content)
    assert backend.requests == []


@pytest.mark.parametrize('outputs', [[], [42], ['final backend text']])
async def test_backend_failure_uses_its_own_text(outputs):
    backend = ScriptedNewtonBackend([
        {'status': 'completed', 'outputs': ['earlier invalid text']},
        {'status': 'failed', 'outputs': outputs, 'error': 'backend failed'},
    ])
    result = await propose_action(backend, model='Newton::test', text_events=['hot'])
    assert result.status == 'failed'
    assert result.raw_text == ('final backend text' if outputs == ['final backend text'] else None)
    assert result.errors[-1].kind == 'backend_failed'
    assert len(backend.requests) == 2


@pytest.mark.parametrize('name,arguments', [
    ('newton_query', {'query': 'q'}),
    ('newton_embed_timeseries', {'channels': [[1.0]]}),
    ('newton_analyze_image', {'question': 'q', 'file_id': 'a.png'}),
    ('newton_propose_action', {'text_events': ['hot']}),
])
async def test_backend_validation_error_stays_opaque(name, arguments):
    sentinel = 'private-response-sentinel'
    error = ValidationError.from_exception_data('BackendResponse', [{
        'type': 'value_error', 'loc': ('response',), 'input': sentinel,
        'ctx': {'error': ValueError(sentinel)},
    }])

    class Backend(MockNewtonBackend):
        async def query(self, request):
            raise error

    async with Client(create_server(Settings(), backend=Backend())) as client:
        result = await client.call_tool(name, arguments)
    assert result.is_error
    assert sentinel not in ' '.join(getattr(c, 'text', '') for c in result.content)


async def test_input_error_translation_preserves_cancellation():
    cancellation = asyncio.CancelledError()

    @_surface_input_errors
    async def cancelled():
        raise cancellation

    with pytest.raises(asyncio.CancelledError) as caught:
        await cancelled()
    assert caught.value is cancellation
