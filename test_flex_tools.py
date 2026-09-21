import asyncio
import copy

import httpx
import pytest
from fastmcp.exceptions import ToolError
from flex_tools import FlexChanges, fingerprint, inspect, flex_state
from server import MeliAPI


def item():
    return {'id': 'MLA123', 'seller_id': 123, 'title': 'Straps negros', 'status': 'active',
            'shipping': {'logistic_type': 'fulfillment', 'tags': ['self_service_in']},
            'price': 19000, 'available_quantity': 15}


class Provider:
    def __init__(self, status=204, apply=True, timeout=False):
        self.item = item()
        self.status, self.apply, self.timeout = status, apply, timeout
        self.calls = []
        self.check = 403
        self.reads = 0
        self.drift = False
        self.fail_after = False

    def handle(self, request):
        self.calls.append((request.method, request.url.path, request.content))
        assert request.headers['Authorization'] == 'Bearer test'
        if request.url.path == '/items/MLA123':
            self.reads += 1
            if self.fail_after and self.reads >= 3:
                return httpx.Response(503)
            if self.drift and self.reads == 2:
                self.item['status'] = 'paused'
            return httpx.Response(200, json=copy.deepcopy(self.item))
        assert request.url.path == '/sites/MLA/shipping/selfservice/items/MLA123'
        if request.method == 'GET':
            return httpx.Response(self.check)
        assert request.method == 'DELETE'
        assert not request.content
        if self.timeout:
            raise httpx.ReadTimeout('timeout')
        if self.apply and 200 <= self.status < 300:
            self.item['shipping']['tags'] = ['self_service_out']
        return httpx.Response(self.status)

    @property
    def writes(self):
        return [c for c in self.calls if c[0] == 'DELETE']


def execute(tmp_path, provider, op='flex-0001', digest=None, confirmation='DESACTIVAR_FLEX'):
    api = MeliAPI('test', transport=httpx.MockTransport(provider.handle))
    return asyncio.run(FlexChanges(tmp_path/'flex.sqlite3').disable(
        api, 123, 'MLA123', digest or fingerprint(item()), op, confirmation))


def test_verified_and_retry_never_writes_twice(tmp_path):
    p = Provider()
    result = execute(tmp_path, p)
    assert result['state'] == 'verified' and result['observed'] is False
    assert result['full_preserved'] is True
    assert p.item['price'] == 19000 and p.item['available_quantity'] == 15
    assert execute(tmp_path, p) == result
    assert len(p.writes) == 1


def test_already_disabled(tmp_path):
    p = Provider()
    p.item['shipping']['tags'] = ['self_service_out']
    assert execute(tmp_path, p, digest=fingerprint(p.item))['state'] == 'unchanged'
    assert not p.writes


@pytest.mark.parametrize('status', [400, 401, 403, 404, 429])
def test_rejection_not_success_or_automatic_retry(tmp_path, status):
    p = Provider(status=status)
    assert execute(tmp_path,p)['state'] == 'rejected'
    execute(tmp_path,p)
    assert len(p.writes) == 1


@pytest.mark.parametrize('status,apply,timeout', [(204, False, False), (503,False,False), (408,False,False), (204,False,True)])
def test_uncertain_blocks_other_operation_ids(tmp_path,status,apply,timeout):
    p = Provider(status,apply,timeout)
    result = execute(tmp_path,p)
    assert result['state'] in ('unknown','verification_mismatch')
    with pytest.raises(ToolError):
        execute(tmp_path,p,op='flex-0002')
    assert len(p.writes) == 1


def test_generic_403_does_not_mean_disabled(tmp_path):
    p = Provider()
    p.item['shipping']['tags'] = []
    with pytest.raises(ToolError):
        execute(tmp_path,p,digest=fingerprint(p.item))
    assert not p.writes


def test_owner_mismatch_and_stale_snapshot(tmp_path):
    p = Provider()
    p.item['seller_id'] = 999
    with pytest.raises(ToolError): execute(tmp_path,p)
    p.item = item()
    p.item['shipping']['free_shipping'] = True
    with pytest.raises(ToolError): execute(tmp_path,p)
    assert not p.writes


def test_drift_before_write(tmp_path):
    p = Provider()
    p.drift = True
    assert execute(tmp_path,p)['state'] == 'precondition_failed'
    assert not p.writes


def test_failed_verification_is_unknown(tmp_path):
    p = Provider()
    p.fail_after = True
    assert execute(tmp_path,p)['state'] == 'unknown'
    assert len(p.writes) == 1


def test_confirmation_required(tmp_path):
    p = Provider()
    with pytest.raises(ToolError): execute(tmp_path,p,confirmation='')
    assert not p.calls


def test_missing_or_conflicting_tags_are_unknown():
    value = item()
    value['shipping']['tags'] = ['self_service_in', 'self_service_out']
    assert flex_state(value) is None
    value['shipping']['tags'] = []
    assert flex_state(value) is None


def test_fallback_204_can_confirm_active(tmp_path):
    p = Provider()
    p.item['shipping']['tags'] = []
    p.check = 204
    assert execute(tmp_path,p,digest=fingerprint(p.item))['state'] == 'verified'


def test_same_operation_cannot_target_changed_request(tmp_path):
    p = Provider()
    execute(tmp_path,p)
    with pytest.raises(ToolError): execute(tmp_path,p,digest='a'*64)
    assert len(p.writes) == 1
