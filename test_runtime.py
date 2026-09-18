import asyncio
import gc
import tracemalloc

import httpx

from runtime_diagnostics import RuntimeDiagnostics
from server import MeliAPI


def test_repeated_meli_cycles_reuse_pool_without_task_or_heap_growth():
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        assert request.headers['authorization'] == 'Bearer test-token'
        return httpx.Response(200, json={'id': calls})

    async def exercise():
        diagnostics = RuntimeDiagnostics()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as pool:
            api = MeliAPI('test-token', client=pool, diagnostics=diagnostics)
            tasks_before = len(asyncio.all_tasks())
            tracemalloc.start()
            baseline = tracemalloc.take_snapshot()
            for _ in range(1000):
                await api.get('/users/me')
            gc.collect()
            growth = sum(stat.size_diff for stat in tracemalloc.take_snapshot().compare_to(baseline, 'filename'))
            tracemalloc.stop()
            assert len(asyncio.all_tasks()) == tasks_before
            assert growth < 500_000
            return diagnostics.snapshot()

    snapshot = asyncio.run(exercise())
    assert calls == 1000
    assert snapshot['meli_requests_total'] == 1000
    assert snapshot['meli_requests_active'] == 0
    assert snapshot['meli_requests_peak_active'] == 1
    assert snapshot['meli_requests_failed'] == 0


def test_diagnostics_contain_only_aggregate_runtime_values():
    snapshot = RuntimeDiagnostics().snapshot()
    assert set(snapshot) == {
        'uptime_seconds', 'rss_bytes', 'rss_change_bytes', 'asyncio_tasks',
        'asyncio_pending_tasks', 'meli_requests_total', 'meli_requests_failed',
        'meli_requests_active', 'meli_requests_peak_active',
    }
    assert snapshot['rss_bytes'] > 0
