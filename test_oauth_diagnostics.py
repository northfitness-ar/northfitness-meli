import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp.server.auth import OAuthProxy, AccessToken
from oauth_diagnostics import DiagnosticOAuthProxy, scope_evidence, CLAIM


@pytest.mark.parametrize('raw', [{}, None, {'scope': None}, {'scope': ['write']}])
def test_missing_provider_scope_is_unknown(raw):
    assert scope_evidence(raw)['available'] is False


def test_only_allowlisted_metadata_is_returned():
    evidence = scope_evidence({'scope': 'read write offline_access secret-value',
                               'access_token': 'secret-token', 'refresh_token': 'secret-refresh'})
    assert evidence['granted_scopes'] == ['offline_access', 'read', 'write']
    assert evidence['ads_write_access'] == 'not_determined'
    assert 'secret' not in str(evidence)
    assert scope_evidence({'scope': 'read'})['write_granted'] is False


@pytest.mark.parametrize('case', ['ok', 'denied', 'rotated', 'missing', 'error'])
def test_diagnostics_preserve_auth_and_bind_to_caller(monkeypatch, case):
    async def run():
        token = AccessToken(token='caller-token', client_id='nf-1', subject='1',
                            scopes=['read'], claims={'original': True})
        monkeypatch.setattr(OAuthProxy, 'load_access_token', AsyncMock(return_value=None if case == 'denied' else token))
        proxy = object.__new__(DiagnosticOAuthProxy)
        monkeypatch.setattr(DiagnosticOAuthProxy, 'jwt_issuer', property(lambda _: SimpleNamespace(verify_token=lambda _: {'jti': 'caller-jti'})))
        proxy._jti_mapping_store = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(upstream_token_id='caller-id')))
        upstream = SimpleNamespace(access_token='other-token' if case == 'rotated' else 'caller-token',
                                   raw_token_data={'scope': 'read write'})
        proxy._upstream_token_store = SimpleNamespace(get=AsyncMock(return_value=None if case == 'missing' else upstream))
        if case == 'error':
            proxy._upstream_token_store.get.side_effect = RuntimeError('secret')
        result = await proxy.load_access_token('reference-token')
        if case == 'denied':
            assert result is None
            proxy._jti_mapping_store.get.assert_not_called()
            return
        assert result.token == token.token and result.scopes == ['read'] and result.subject == '1'
        assert result.claims['original'] is True
        assert result.claims[CLAIM]['available'] is (case == 'ok')
        assert 'secret' not in str(result.claims)
        proxy._jti_mapping_store.get.assert_awaited_once_with(key='caller-jti')
        proxy._upstream_token_store.get.assert_awaited_once_with(key='caller-id')
    asyncio.run(run())
