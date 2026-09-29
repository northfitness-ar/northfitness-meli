import asyncio
import hashlib
import hmac
import base64
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qs

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from monitor import Monitor, register
from monitor_auth import AuthError, RateLimited, COOKIE, REMEMBER_SECONDS, password_hash

PASSWORD = 'Private test password 12345!'


def fields(link, password=PASSWORD, remember=True):
    parts = parse_qs(urlsplit(link['url']).fragment)
    return {'username': parts['user'][0], 'token': parts['activate'][0],
            'password': password, 'remember': remember}


def activation(auth, name='salvador'):
    return fields(next(link for link in auth.activation_links()['activation_links'] if link['username']==name))


def request(token):
    return SimpleNamespace(cookies={COOKIE: token})


@pytest.fixture
def monitor(tmp_path):
    return Monitor(tmp_path, None, 'test', 'https://nf.example', 'x'*48)


def test_two_accounts_passwords_hashed_sessions_private_and_persistent(monitor, tmp_path):
    auth = monitor.auth
    links = auth.activation_links()
    assert set(links['users']) == {'salvador', 'maxi'}
    assert '#' not in links['url']
    logins = [auth.authenticate(fields(link), 'ip', activation=True) for link in links['activation_links']]
    assert {auth.user(request(login['token'])) for login in logins} == {'salvador', 'maxi'}
    assert auth.activation_links()['activation_links'] == []
    with auth.db() as c:
        stored = c.execute('SELECT password_hash FROM monitor_users').fetchall()
        sessions = c.execute('SELECT hash FROM monitor_logins').fetchall()
    assert stored[0] != stored[1]  # independent random salts even for equal passwords
    assert all(PASSWORD not in row[0] for row in stored)
    assert all(login['token'] not in str(sessions) for login in logins)
    restarted = Monitor(tmp_path, None, 'test', 'https://nf.example').auth
    assert restarted.user(request(logins[0]['token'])) == logins[0]['username']
    with pytest.raises(AuthError):
        restarted.authenticate({'username':'salvador','password':'Wrong password long enough'}, 'ip')
    login = restarted.authenticate({'username':'SALVADOR','password':PASSWORD}, 'ip')
    assert restarted.user(request(login['token'])) == 'salvador'
    assert login['seconds']==8*3600 and not login['remember']


def test_activation_wrong_user_expiry_replacement_and_replay(monitor):
    auth=monitor.auth
    old=activation(auth)
    fresh=activation(auth)
    with pytest.raises(AuthError):auth.authenticate(old,'ip',True)
    with pytest.raises(AuthError):auth.authenticate(dict(fresh,username='maxi'),'ip',True)
    with auth.db() as c:c.execute('UPDATE monitor_activations SET expires=?',(time.time()-1,))
    with pytest.raises(AuthError):auth.authenticate(fresh,'ip',True)
    fresh=activation(auth)
    auth.authenticate(fresh,'ip',True)
    with pytest.raises(AuthError):auth.authenticate(fresh,'ip',True)
    with pytest.raises(AuthError):auth.authenticate(dict(fresh,password='Another long password'),'ip',True)
    assert auth.authenticate({'username':'salvador','password':PASSWORD},'ip')['username']=='salvador'


def test_activation_consumed_atomically(monitor):
    payload=activation(monitor.auth)
    def attempt():
        try:return monitor.auth.authenticate(payload,'ip',True)
        except AuthError:return None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:attempt(),range(2)))
    assert sum(r is not None for r in results)==1


def test_logout_expiration_and_reset_revoke_only_target(monitor):
    auth=monitor.auth
    links=auth.activation_links()['activation_links']
    sessions={link['username']:auth.authenticate(fields(link),'ip',True)['token'] for link in links}
    other=auth.authenticate({'username':'salvador','password':PASSWORD},'ip')['token']
    auth.logout(request(other))
    assert not auth.user(request(other))
    assert auth.user(request(sessions['salvador']))=='salvador'
    reset=auth.reset('salvador')
    assert not auth.user(request(sessions['salvador']))
    assert auth.user(request(sessions['maxi']))=='maxi'
    with pytest.raises(AuthError):auth.authenticate({'username':'salvador','password':PASSWORD},'ip')
    auth.authenticate(fields(reset,'Different private password!'),'ip',True)
    with pytest.raises(AuthError):auth.authenticate({'username':'salvador','password':PASSWORD},'ip')
    with auth.db() as c:c.execute('UPDATE monitor_logins SET expires=?',(time.time()-1,))
    assert not auth.user(request(sessions['maxi']))


def test_throttle_persists_across_restart_and_expires(monitor,tmp_path,monkeypatch):
    auth=monitor.auth
    # Malformed attempts still count; no expensive KDF necessary to test limit.
    for _ in range(10):
        with pytest.raises(AuthError):auth.authenticate({'username':'salvador','password':'short'},'ip')
    restarted=Monitor(tmp_path,None,'test','https://nf.example').auth
    with pytest.raises(RateLimited):restarted.authenticate({'username':'salvador','password':PASSWORD},'different-ip')
    now=time.time()
    monkeypatch.setattr('monitor_auth.time.time',lambda:now+901)
    with pytest.raises(AuthError) as err:restarted.authenticate({'username':'salvador','password':'short'},'ip')
    assert not isinstance(err.value,RateLimited)


def test_throttle_ip_global_and_parallel_reservations(monitor):
    auth=monitor.auth
    def attempt(_):
        try:auth.throttle('salvador','one-ip');return True
        except RateLimited:return False
    with ThreadPoolExecutor(max_workers=12) as pool:results=list(pool.map(attempt,range(12)))
    assert sum(results)==10
    with auth.db() as c:c.execute('DELETE FROM monitor_attempts')
    for _ in range(30):auth.throttle('unknown','one-ip')
    with pytest.raises(RateLimited):auth.throttle('unknown','one-ip')
    for n in range(70):auth.throttle('unknown',str(n))
    with pytest.raises(RateLimited):auth.throttle('unknown','new-ip')


@pytest.mark.parametrize('password',[None,True,{},'a'*14,'a'*129])
def test_invalid_password_types_and_length(monitor,password):
    payload=activation(monitor.auth)
    with pytest.raises(AuthError):monitor.auth.authenticate(dict(payload,password=password),'ip',True)
    assert not monitor.auth.user(request('invalid'))


class FakeMCP:
    def __init__(self):self.routes=[];self.tools={}
    def custom_route(self,path,methods):
        def register_route(fn):self.routes.append(Route(path,fn,methods=methods));return fn
        return register_route
    def tool(self,**kwargs):
        def register_tool(fn):self.tools[fn.__name__]=fn;return fn
        return register_tool


@pytest.fixture
def web(tmp_path):
    mcp=FakeMCP()
    monitor=register(mcp,lambda:None,None,'test',tmp_path,{'BASE_URL':'https://nf.example','JWT_SIGNING_KEY':'x'*48})
    async def period(*args,**kwargs):return {'private':'financial-data'}
    monitor.period=period;monitor.compare=period
    with TestClient(Starlette(routes=mcp.routes),base_url='https://nf.example') as client:
        yield client,monitor,mcp


def test_real_http_login_activation_cookie_flags_data_logout(web):
    client,monitor,mcp=web
    origin={'Origin':'https://nf.example'}
    for path in ['/monitor/data','/monitor/compare','/monitor/me']:
        assert client.get(path).status_code==401
    page=client.get('/monitor',follow_redirects=False)
    assert page.status_code==303 and page.headers['location']=='/monitor/login'
    assert 'Ingresá a tu cuenta' in client.get('/monitor/login').text
    links=mcp.tools['nf_monitor_abrir']()
    payload=fields(links['activation_links'][0])
    response=client.post('/monitor/activate',json=payload,headers=origin)
    assert response.status_code==200
    cookie=response.headers.get_list('set-cookie')[0]
    for flag in ['Secure','HttpOnly','SameSite=strict','Path=/monitor',f'Max-Age={REMEMBER_SECONDS}']:
        assert flag in cookie
    assert client.get('/monitor/data').json()=={'private':'financial-data'}
    assert client.get('/monitor/compare').json()=={'private':'financial-data'}
    assert 'Monitor de rentabilidad' in client.get('/monitor').text
    assert client.get('/monitor/me').json()=={'username':payload['username']}
    old=client.cookies.get(COOKIE)
    assert client.post('/monitor/logout',headers=origin).status_code==200
    assert client.get('/monitor/data').status_code==401
    assert not monitor.auth.user(request(old))
    # Valid credentials work without an activation link; password isn't echoed.
    response=client.post('/monitor/login',json=dict(payload,remember=False),headers=origin)
    assert response.status_code==200 and PASSWORD not in response.text
    assert 'Max-Age' not in response.headers.get_list('set-cookie')[0]
    assert 'no-store' in response.headers['cache-control']
    assert 'noindex' in response.headers['x-robots-tag']


def test_http_csrf_malformed_legacy_token_and_cookie_rejected(web):
    client,monitor,_=web
    origin={'Origin':'https://nf.example'}
    payload=activation(monitor.auth)
    for path in ['/monitor/login','/monitor/activate','/monitor/logout']:
        assert client.post(path,json=payload).status_code==403
        assert client.post(path,json=payload,headers={'Origin':'https://evil.example'}).status_code==403
    for body in [[],None,'string']:
        assert client.post('/monitor/login',content=__import__('json').dumps(body),headers=dict(origin,**{'Content-Type':'application/json'})).status_code==400
    assert client.post('/monitor/login',content='invalid',headers=dict(origin,**{'Content-Type':'application/json'})).status_code==400
    assert client.post('/monitor/login',data={'username':'salvador'},headers=origin).status_code==400
    assert client.post('/monitor/login',json={'password':'x'*5000},headers=origin).status_code==413
    legacy=base64.urlsafe_b64encode(hmac.new(b'x'*48,b'northfitness-monitor-permanent-link-v1',hashlib.sha256).digest()).rstrip(b'=').decode()
    assert client.post('/monitor/session',json={'token':legacy},headers=origin).status_code==401
    client.cookies.set('nf_monitor','legacy')
    assert client.get('/monitor/data').status_code==401
    # Setup is never a public GET or a username-only registration endpoint.
    assert client.get('/monitor/activate').status_code==405
    assert client.post('/monitor/activate',json=dict(payload,token='x'*43),headers=origin).status_code==401


def test_owner_gate_before_setup_and_reset(tmp_path):
    mcp=FakeMCP()
    def denied():raise PermissionError('No owner authentication')
    monitor=register(mcp,denied,None,'test',tmp_path,{'BASE_URL':'https://nf.example'})
    with pytest.raises(PermissionError):mcp.tools['nf_monitor_abrir']()
    with pytest.raises(PermissionError):mcp.tools['nf_monitor_usuario_restablecer']('salvador','RESTABLECER_ACCESO_MONITOR')
    with monitor.db() as c:assert c.execute('SELECT count(*) FROM monitor_activations').fetchone()[0]==0
