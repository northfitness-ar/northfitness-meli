import asyncio
import copy
import json
from datetime import datetime, timedelta

import httpx
import pytest
from agents_audit import Audit, AuditError, TZ, closed_day, project_snapshot

DAY = (datetime.now(TZ).date()-timedelta(days=1)).isoformat()
SNAPSHOT = {'day': DAY, 'stale': False, 'orders_count': 1, 'net_estimate': None,
            'coverage_percent': 0, 'buyer': 'NEVER_FORWARD',
            'orders': [{'id': '123', 'net': None, 'revenue': '12.10', 'buyer': 'NEVER_FORWARD',
                        'missing': ['tax_adjustment']}]}

class Monitor:
    async def snapshot(self, day, force=False):
        return dict(copy.deepcopy(SNAPSHOT), day=day)


def make(tmp_path, handler):
    return Audit({'OPENAI_API_KEY': 'SECRET', 'NF_AGENTS_MODEL': 'test-model',
                  'NF_AGENTS_ENABLED': 'true'}, tmp_path, Monitor(), None, '237699011', httpx.MockTransport(handler))


def seed(a, state='running'):
    a.save({'day': DAY, 'state': state, 'session_id': 'sess_test', 'snapshot': SNAPSHOT,
            'evidence_sha256': 'hash'})


def test_snapshot_redacts_buyer_and_keeps_unknowns():
    result = project_snapshot(SNAPSHOT)
    assert 'NEVER_FORWARD' not in json.dumps(result)
    assert result['net_estimate'] is None and result['orders'][0]['net'] is None


@pytest.mark.parametrize('change', [{'stale': True}, {'orders_count': 2},
    {'orders_count': 2, 'orders': [SNAPSHOT['orders'][0], SNAPSHOT['orders'][0]]}])
def test_incomplete_snapshot_blocks_session(change):
    with pytest.raises(AuditError):
        project_snapshot(dict(SNAPSHOT, **change))


def test_today_blocked():
    with pytest.raises(AuditError):
        closed_day(datetime.now(TZ).date().isoformat())


def test_duplicate_start_and_restart_do_not_repeat_post(tmp_path):
    requests = []
    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        assert body['environment'] == {'type': 'none'}
        assert all(t['type'] == 'function' for t in body['agent']['tools'])
        assert 'SECRET' not in request.content.decode()
        return httpx.Response(200, json={'id': 'sess_test'})
    a = make(tmp_path, handler)
    first = asyncio.run(a.start(DAY))
    b = make(tmp_path, handler)
    second = asyncio.run(b.start(DAY))
    assert first == second and len(requests) == 1


def test_unknown_post_is_reserved_across_restart(tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout('SECRET', request=request)
    a = make(tmp_path, handler)
    result = asyncio.run(a.start(DAY))
    assert result['state'] == 'creation_unknown' and 'SECRET' not in json.dumps(result)
    asyncio.run(make(tmp_path, handler).start(DAY))
    assert len(calls) == 1


def test_disabled_does_not_contact_provider(tmp_path):
    a = make(tmp_path, lambda r: pytest.fail('No network allowed'))
    a.env['NF_AGENTS_ENABLED'] = 'false'
    with pytest.raises(AuditError):
        asyncio.run(a.start(DAY))


def test_seven_session_limit(tmp_path):
    a = make(tmp_path, lambda r: pytest.fail('Quota must block'))
    for i in range(2, 9):
        a.save({'day': (datetime.now(TZ).date()-timedelta(days=i)).isoformat(), 'state': 'creation_unknown'})
    with pytest.raises(AuditError, match='siete'):
        asyncio.run(a.start(DAY))


def test_forbidden_tools_and_out_of_scope_order_never_execute(tmp_path):
    a = make(tmp_path, lambda r: pytest.fail('No network'))
    seed(a)
    for i, (name, args) in enumerate([('nf_precio_fijar', {}), ('read_order_financial_evidence', {'order_id': '999'})]):
        event = asyncio.run(a.tool_result(a.get(DAY), {'type': 'function_call', 'turn_id': 'turn_1', 'call_id': 'call_'+str(i), 'name': name, 'arguments': args}))
        assert event['success'] is False


def test_saved_function_result_reused_and_identity_checked(tmp_path):
    a = make(tmp_path, lambda r: pytest.fail('No network'))
    seed(a)
    action = {'type': 'function_call', 'turn_id': 'turn_1', 'call_id': 'call_1', 'name': 'read_audit_orders', 'arguments': {'offset': 0}}
    first = asyncio.run(a.tool_result(a.get(DAY), action))
    assert json.loads(first['output'])['orders'][0]['net'] is None
    assert asyncio.run(make(tmp_path, lambda r: None).tool_result(a.get(DAY), action)) == first
    with pytest.raises(AuditError, match='changed_call_identity'):
        asyncio.run(a.tool_result(a.get(DAY), dict(action, arguments={'offset': 1})))


def test_idle_is_not_completion(tmp_path):
    def handler(r):
        return httpx.Response(200, json={'status': 'idle', 'required_actions': []} if not r.url.path.endswith('/turns') else {'data': [{'id': 'turn_1', 'status': 'in_progress'}], 'has_more': False})
    a = make(tmp_path, handler); seed(a)
    assert asyncio.run(a.advance(DAY))['state'] == 'running'


def test_completed_turn_fetches_all_pages_and_only_final_output(tmp_path):
    requests = []
    def handler(r):
        requests.append(str(r.url))
        if r.url.path.endswith('/turns'):
            return httpx.Response(200, json={'data': [{'id': 'turn_1', 'status': 'completed', 'usage': {'total_tokens': 42}}], 'has_more': False})
        if r.url.path.endswith('/items'):
            if r.url.params.get('after'):
                return httpx.Response(200, json={'data': [{'id': 'msg_2', 'role': 'assistant', 'phase': 'final_answer', 'turn_id': 'turn_1', 'status': 'completed', 'content': [{'type': 'output_text', 'text': 'Pendiente de conciliación.'}]}], 'has_more': False})
            return httpx.Response(200, json={'data': [{'id': 'msg_1', 'role': 'assistant', 'phase': 'commentary'}], 'has_more': True})
        return httpx.Response(200, json={'status': 'idle', 'required_actions': []})
    a = make(tmp_path, handler); seed(a)
    result = asyncio.run(a.advance(DAY))
    assert result['state'] == 'completed_advisory'
    assert result['report'] == 'Pendiente de conciliación.' and result['usage']['total_tokens'] == 42
    assert any('after=msg_1' in r for r in requests)


def test_unauthorized_diagnostic_sanitizes_provider_error(tmp_path):
    a = make(tmp_path, lambda r: httpx.Response(403, text='SECRET'))
    result = asyncio.run(a.diagnose())
    assert result['session_read_access'] is False and result['error'] == 'openai_http_403'
    assert 'SECRET' not in json.dumps(result)
