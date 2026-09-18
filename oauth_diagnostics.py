"""Read-only OAuth evidence for the authenticated session (FastMCP 3.4.7)."""
from fastmcp.server.auth import OAuthProxy

CLAIM = 'nf_oauth_evidence'


def scope_evidence(raw):
    # Never infer granted scopes from requested/default/local verifier scopes.
    value = raw.get('scope') if isinstance(raw, dict) else None
    if not isinstance(value, str):
        return {'available': False, 'reason': 'provider_scope_not_recorded'}
    scopes = set(value.split())
    return {'available': True, 'source': 'stored_provider_token_response',
            'granted_scopes': sorted(scopes & {'read', 'write', 'offline_access'}),
            'write_granted': 'write' in scopes,
            'ads_write_access': 'not_determined'}


class DiagnosticOAuthProxy(OAuthProxy):
    async def load_access_token(self, token):
        validated = await super().load_access_token(token)
        if validated is None:
            return None
        evidence = {'available': False, 'reason': 'provider_scope_not_recorded'}
        try:
            # Resolve only the already validated caller's mapping, never enumerate
            # token storage. Do not change tokens, scopes, or authorization logic.
            payload = self.jwt_issuer.verify_token(token)
            mapping = await self._jti_mapping_store.get(key=payload['jti'])
            if mapping:
                upstream = await self._upstream_token_store.get(key=mapping.upstream_token_id)
                if upstream and upstream.access_token == validated.token:
                    evidence = scope_evidence(upstream.raw_token_data)
        except Exception:
            # Diagnostics must neither disclose exceptions nor break authentication.
            pass
        return validated.model_copy(update={'claims': {**(validated.claims or {}), CLAIM: evidence}})
