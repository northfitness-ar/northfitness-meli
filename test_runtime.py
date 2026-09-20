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
        'open_file_descriptors',
    }
    assert snapshot['rss_bytes'] > 0


def test_sqlite_connection_closes_commits_and_rolls_back(tmp_path):
    import sqlite3
    from server import sqlite_transaction
    path=str(tmp_path/'transactions.db')
    with sqlite_transaction(path) as c:
        c.execute('CREATE TABLE t (n INTEGER)')
        c.execute('INSERT INTO t VALUES (1)')
    import pytest
    with pytest.raises(sqlite3.ProgrammingError):c.execute('SELECT 1')
    with pytest.raises(RuntimeError):
        with sqlite_transaction(path) as failed:
            failed.execute('INSERT INTO t VALUES (2)')
            raise RuntimeError('rollback')
    with pytest.raises(sqlite3.ProgrammingError):failed.execute('SELECT 1')
    with sqlite_transaction(path) as check:
        assert check.execute('SELECT n FROM t').fetchall()==[(1,)]

def test_monitor_and_worker_db_close_without_gc(tmp_path):
    import gc,os,sqlite3
    from types import SimpleNamespace
    from monitor import Monitor
    from support_auto import AutoSupport
    import pytest
    owner=SimpleNamespace(path=str(tmp_path/'cycles.db'))
    enabled=gc.isenabled()
    try:
        gc.disable()
        before=len(os.listdir('/proc/self/fd')) if os.path.exists('/proc/self/fd') else None
        for factory in (Monitor.db, AutoSupport.db):
            for _ in range(1000):
                with factory(owner) as c:c.execute('SELECT 1').fetchone()
            with pytest.raises(sqlite3.ProgrammingError):c.execute('SELECT 1')
        if before is not None:assert len(os.listdir('/proc/self/fd'))==before
    finally:
        if enabled:gc.enable()
