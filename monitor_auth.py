"""Two private dashboard accounts, independent of Mercado Libre/MCP credentials."""
import hashlib
import secrets
import time
from urllib.parse import urlencode

USERS = ('salvador', 'maxi')
COOKIE = '__Secure-nf_monitor_v2'
SESSION_SECONDS = 8 * 3600
REMEMBER_SECONDS = 30 * 86400
ACTIVATION_SECONDS = 86400
ITERATIONS = 600_000
WINDOW = 15 * 60


class AuthError(ValueError):
    status = 401


class RateLimited(AuthError):
    status = 429


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    derived = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), ITERATIONS)
    return salt + ':' + derived.hex()


class MonitorAuth:
    def __init__(self, db, base_url):
        self.db, self.base_url = db, base_url
        # A dummy hash gives unknown/inactive usernames the same KDF work.
        self.dummy = password_hash(secrets.token_urlsafe(32))
        with self.db() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS monitor_users (
                    username TEXT PRIMARY KEY, password_hash TEXT, updated_at REAL);
                CREATE TABLE IF NOT EXISTS monitor_logins (
                    hash TEXT PRIMARY KEY, username TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS monitor_activations (
                    hash TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS monitor_attempts (
                    key TEXT PRIMARY KEY, count INTEGER NOT NULL, expires REAL NOT NULL);
            ''')
            for username in USERS:
                c.execute('INSERT OR IGNORE INTO monitor_users VALUES (?,NULL,?)', (username, time.time()))
            # Legacy bearer links and their cookies never grant access after migration.
            c.execute('DELETE FROM sessions')

    def activation_links(self):
        """Owner-authenticated MCP only; never expose this method as a public route."""
        now = time.time()
        links = []
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            for username, stored in c.execute('SELECT username,password_hash FROM monitor_users ORDER BY username').fetchall():
                if stored is not None:
                    continue
                token = secrets.token_urlsafe(32)
                c.execute('DELETE FROM monitor_activations WHERE username=?', (username,))
                c.execute('INSERT INTO monitor_activations VALUES (?,?,?)',
                          (digest(token), username, now + ACTIVATION_SECONDS))
                links.append({'username': username, 'url': self.base_url + '/monitor/login#' +
                              urlencode({'activate': token, 'user': username}),
                              'expires_in_seconds': ACTIVATION_SECONDS})
        return {'url': self.base_url + '/monitor', 'authentication': 'password',
                'users': list(USERS), 'activation_links': links}

    def reset(self, username):
        if username not in USERS:
            raise ValueError('Usuario de monitor inválido.')
        now = time.time()
        token = secrets.token_urlsafe(32)
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('UPDATE monitor_users SET password_hash=NULL,updated_at=? WHERE username=?', (now, username))
            c.execute('DELETE FROM monitor_logins WHERE username=?', (username,))
            c.execute('DELETE FROM monitor_activations WHERE username=?', (username,))
            c.execute('DELETE FROM monitor_attempts WHERE key=?', ('user:' + username,))
            c.execute('INSERT INTO monitor_activations VALUES (?,?,?)', (digest(token), username, now + ACTIVATION_SECONDS))
        return {'username': username, 'url': self.base_url + '/monitor/login#' +
                urlencode({'activate': token, 'user': username}), 'expires_in_seconds': ACTIVATION_SECONDS}

    def throttle(self, username, address):
        """Persisted, atomic limits; raw user-controlled proxy headers are not trusted."""
        now = time.time()
        keys = [('global', 100), ('ip:' + digest(address), 30)]
        if username in USERS:
            keys.append(('user:' + username, 10))
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('DELETE FROM monitor_attempts WHERE expires<=?', (now,))
            for key, limit in keys:
                row = c.execute('SELECT count FROM monitor_attempts WHERE key=?', (key,)).fetchone()
                if row and row[0] >= limit:
                    raise RateLimited('Demasiados intentos. Esperá 15 minutos y volvé a intentar.')
            for key, _ in keys:
                c.execute('INSERT INTO monitor_attempts VALUES (?,1,?) ON CONFLICT(key) DO UPDATE SET count=count+1',
                          (key, now + WINDOW))

    def authenticate(self, payload, address, activation=False):
        username = payload.get('username', '')
        username = username.strip().lower() if isinstance(username, str) and len(username) <= 100 else ''
        self.throttle(username, address)
        password = payload.get('password')
        if not isinstance(password, str) or not 15 <= len(password) <= 128:
            raise AuthError('La contraseña debe tener entre 15 y 128 caracteres.' if activation
                            else 'Usuario o contraseña incorrectos.')
        remember = payload.get('remember') is True
        if activation:
            token = payload.get('token')
            if not isinstance(token, str) or not 32 <= len(token) <= 200:
                raise AuthError('Enlace inválido, vencido o ya utilizado. Pedí uno nuevo desde NorthFitness.')
            with self.db() as c:
                row = c.execute('SELECT a.username,a.expires,u.password_hash FROM monitor_activations a '
                                'JOIN monitor_users u ON u.username=a.username WHERE a.hash=?', (digest(token),)).fetchone()
            if not row or row[0] != username or row[1] <= time.time() or row[2] is not None:
                raise AuthError('Enlace inválido, vencido o ya utilizado. Pedí uno nuevo desde NorthFitness.')
            stored = password_hash(password)
            with self.db() as c:
                c.execute('BEGIN IMMEDIATE')
                # Re-check after the KDF: a link may be consumed/reset concurrently.
                changed = c.execute('DELETE FROM monitor_activations WHERE hash=? AND username=? AND expires>?',
                                    (digest(token), username, time.time())).rowcount
                if changed != 1:
                    raise AuthError('Enlace inválido, vencido o ya utilizado. Pedí uno nuevo desde NorthFitness.')
                changed = c.execute('UPDATE monitor_users SET password_hash=?,updated_at=? '
                                    'WHERE username=? AND password_hash IS NULL', (stored, time.time(), username)).rowcount
                if changed != 1:
                    raise AuthError('Enlace ya utilizado.')
                c.execute('DELETE FROM monitor_logins WHERE username=?', (username,))
                return self._session(c, username, remember)
        with self.db() as c:
            row = c.execute('SELECT password_hash FROM monitor_users WHERE username=?', (username,)).fetchone()
        expected = row[0] if row and row[0] else self.dummy
        valid = secrets.compare_digest(password_hash(password, expected.split(':')[0]), expected)
        if not valid or not row or not row[0]:
            raise AuthError('Usuario o contraseña incorrectos.')
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            # A reset racing with password verification must not create a session.
            current = c.execute('SELECT password_hash FROM monitor_users WHERE username=?', (username,)).fetchone()
            if not current or current[0] != expected:
                raise AuthError('Usuario o contraseña incorrectos.')
            return self._session(c, username, remember)

    def _session(self, c, username, remember):
        now = time.time()
        seconds = REMEMBER_SECONDS if remember else SESSION_SECONDS
        token = secrets.token_urlsafe(32)
        c.execute('DELETE FROM monitor_logins WHERE expires<=?', (now,))
        # Bound retained sessions per account without affecting the other partner.
        c.execute('DELETE FROM monitor_logins WHERE username=? AND hash NOT IN '
                  '(SELECT hash FROM monitor_logins WHERE username=? ORDER BY expires DESC LIMIT 19)', (username, username))
        c.execute('INSERT INTO monitor_logins VALUES (?,?,?)', (digest(token), username, now + seconds))
        c.execute('DELETE FROM monitor_attempts WHERE key=?', ('user:' + username,))
        return {'token': token, 'username': username, 'seconds': seconds, 'remember': remember}

    def user(self, request):
        token = request.cookies.get(COOKIE, '')
        if not token or len(token) > 200:
            return None
        with self.db() as c:
            row = c.execute('SELECT s.username FROM monitor_logins s JOIN monitor_users u ON u.username=s.username '
                            'WHERE s.hash=? AND s.expires>? AND u.password_hash IS NOT NULL', (digest(token), time.time())).fetchone()
        return row[0] if row else None

    def logout(self, request):
        token = request.cookies.get(COOKIE, '')
        if token and len(token) <= 200:
            with self.db() as c:
                c.execute('DELETE FROM monitor_logins WHERE hash=?', (digest(token),))
