"""GP3 cloud client. Standard library only. No network requests on import.

Set GP3_BASE_URL to the teaching team's HTTPS address before the first call.
Each new successful call consumes one group pull. Calls are never automatic.
After a timeout, repeating the same call in the same Python session safely
reuses its request ID and counter. Preserve the recovery fields in an
UncertainPullError before restarting Python.
"""
import hashlib
import json
import os
import threading
import uuid
from operator import index
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

BASE_URL = os.environ.get('GP3_BASE_URL', '').rstrip('/')
_sessions = {}
_registry_lock = threading.Lock()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UncertainPullError(RuntimeError):
    def __init__(self, request_id, expected_pull):
        self.request_id, self.expected_pull = request_id, expected_pull
        super().__init__('No reliable response was received. Retry the SAME arm with '
                         f'request_id={request_id!r}, expected_pull={expected_pull}. '
                         'Do not create a new request until this one is resolved.')


def configure(base_url):
    global BASE_URL
    url = urlsplit(base_url)
    if url.scheme != 'https' or not url.netloc or url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
        raise ValueError('Use only the HTTPS server address supplied by the teaching team.')
    with _registry_lock:
        if any(session['pending'] for session in _sessions.values()):
            raise RuntimeError('Resolve pending requests before changing the server.')
        BASE_URL = base_url.rstrip('/')
        _sessions.clear()


def _session(username, password):
    if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
        raise ValueError('Enter the group username and password.')
    if not BASE_URL:
        raise ValueError('Call configure("https://...") with the address from the teaching team first.')
    url = urlsplit(BASE_URL)
    if url.scheme != 'https' or not url.netloc or url.username or url.password or url.query or url.fragment or url.path:
        raise ValueError('Invalid GP3 server address.')
    key = (BASE_URL, username, hashlib.sha256(password.encode()).hexdigest())
    with _registry_lock:
        return _sessions.setdefault(key, {'lock': threading.RLock(), 'next': None, 'pending': None})


def _request(username, password, path, timeout, data=None):
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 60:
        raise ValueError('timeout must be between 0 and 60 seconds.')
    body = json.dumps(data).encode() if data is not None else None
    headers = {'Accept': 'application/json', 'Authorization': 'Bearer ' + password,
               'X-Group': username, 'Content-Type': 'application/json'}
    req = Request(BASE_URL + path, data=body, headers=headers)
    try:
        with build_opener(_NoRedirect()).open(req, timeout=timeout) as response:
            return response.status, json.loads(response.read(8192))
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read(8192))
        except (ValueError, UnicodeError):
            payload = {'message': 'The server rejected the request.'}
        finally:
            exc.close()
        return exc.code, payload
    except (URLError, TimeoutError, OSError, ValueError, UnicodeError):
        # Never expose request headers, passwords, or credential-bearing URLs.
        raise ConnectionError('The response could not be verified.') from None


def status(user_group, secret_key, *, timeout=10.0):
    session = _session(user_group, secret_key)
    with session['lock']:
        code, data = _request(user_group, secret_key, '/v1/status', timeout)
        if code != 200 or not isinstance(data, dict) or 'PullsUsed' not in data:
            raise RuntimeError('Could not retrieve group status. Check access with the teaching team.')
        if session['pending'] is None:
            session['next'] = data['PullsUsed']
        return data


def pull(user_group, secret_key, arm, *, timeout=10.0, request_id=None, expected_pull=None):
    try:
        arm_id = index(arm)
    except TypeError:
        raise ValueError('arm must be an integer from 0 to 23.') from None
    if isinstance(arm, bool) or not 0 <= arm_id < 24:
        raise ValueError('arm must be an integer from 0 to 23.')
    session = _session(user_group, secret_key)
    with session['lock']:
        if (request_id is None) != (expected_pull is None):
            raise ValueError('For recovery, provide BOTH request_id and expected_pull.')
        if request_id is not None:
            try:
                if str(uuid.UUID(request_id)) != request_id:
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise ValueError('request_id must be a canonical UUID.') from None
            if type(expected_pull) is not int or not 0 <= expected_pull < 1_000_000:
                raise ValueError('Invalid recovery counter.')
            payload = {'arm': arm_id, 'expected_pull': expected_pull, 'request_id': request_id}
            if session['pending'] is not None and session['pending'] != payload:
                raise RuntimeError('Resolve the existing pending request first.')
            session['pending'] = payload
        elif session['pending'] is not None:
            payload = session['pending']
            if payload['arm'] != arm_id:
                raise RuntimeError('Retry the pending arm before choosing another arm.')
        else:
            if session['next'] is None:
                status(user_group, secret_key, timeout=timeout)
            if session['next'] >= 1_000_000:
                raise RuntimeError('Your group has used all available pulls.')
            payload = {'arm': arm_id, 'expected_pull': session['next'], 'request_id': str(uuid.uuid4())}
            session['pending'] = payload
        try:
            code, data = _request(user_group, secret_key, '/v1/pull', timeout, payload)
        except ConnectionError:
            raise UncertainPullError(payload['request_id'], payload['expected_pull']) from None
        if code == 200:
            result = data.get('result') if isinstance(data, dict) else None
            if (not isinstance(result, dict) or not {'Arm', 'Reward', 'NetReward', 'Pull'}.issubset(result)
                    or result['Arm'] != str(arm_id) or result['Pull'] != payload['expected_pull']):
                raise UncertainPullError(payload['request_id'], payload['expected_pull'])
            session['next'] = result['Pull'] + 1
            session['pending'] = None
            return result
        if code in (400, 401, 403, 409):
            session['pending'], session['next'] = None, None
            message = data.get('message', 'Request rejected.') if isinstance(data, dict) else 'Request rejected.'
            raise RuntimeError(message)
        # Rate-limited or uncertain responses retain the exact request for retry.
        raise UncertainPullError(payload['request_id'], payload['expected_pull'])
