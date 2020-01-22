from __future__ import absolute_import

import datetime
import hashlib
import inspect
import logging
import urlparse
import xml.etree.ElementTree as et

import requests
from flask import Blueprint, jsonify, json, request, session, current_app, abort, Response
from six import text_type

from . import ldap_auth
from dim import db
from dim.errors import DimError
from dim.models import User
from dim.rpc import TRPC, RPC


jsonrpc = Blueprint('jsonrpc', __name__)

_options_pos = dict(
    group_grant_access=None,
    group_revoke_access=None)


def _update_options_pos():
    for name, func in inspect.getmembers(RPC, inspect.ismethod):
        if name.startswith('_'):
            continue
        required_args = func.__func__.__code__.co_argcount
        if func.__func__.__defaults__:
            required_args -= len(func.__func__.__defaults__)
        if name not in _options_pos:
            _options_pos[name] = required_args


_update_options_pos()


def _check_credentials(username, password):
    method = current_app.config['AUTHENTICATION_METHOD']
    if method == 'ldap':
        return ldap_auth.check_credentials(username, password)
    elif method is None:
        return True
    else:
        logging.error(u"Invalid AUTHENTICATION_METHOD: %r" % method)
    return False


def _do_login(authenticated, username, tool):
    if authenticated:
        # Add the user to the database after the first successful login
        if User.query.filter_by(username=username).count() == 0:
            db.session.add(User(username))
            db.session.commit()
            logging.debug(u'Created user %s' % username)
        session.permanent = request.form.get('permanent_session', False)
        session['username'] = username
        if tool:
            session['tool'] = tool.lower()
    elif 'username' in session:
        session.pop('username')
    if tool and tool.lower() == 'dimfe':
        return index()
    if authenticated:
        return 'Ok'
    return 'Invalid credentials', 401


def _compute_sign(username, salt, secret_key):
    return hashlib.md5(username + salt + secret_key).hexdigest()


def _check_tool_login(username, tool, salt, sign):
    secret_key = current_app.config.get('SECRET_KEY_' + tool.upper(), None)
    authenticated = False
    if secret_key:
        authenticated = sign == _compute_sign(username, salt, secret_key)
    else:
        logging.info(u'Invalid tool %s tried to login user %s', tool, username)
        return False
    if not authenticated:
        logging.info(u'Tool %s login for user %s failed', tool, username)
    return authenticated


@jsonrpc.route('/login', methods=['POST'])
def login():
    if 'username' not in request.form:
        return 'Required parameter is missing: username', 400
    if 'password' not in request.form and \
       len([x for x in ['salt', 'tool', 'sign'] if x not in request.form]) != 0:
        return 'Required parameters missing: password or sign, salt and tool', 400
    authenticated = False
    username = request.form.get('username')
    tool = None
    if 'password' in request.form:
        authenticated = _check_credentials(username, request.form.get('password', None))
    else:
        tool = request.form.get('tool')
        authenticated = _check_tool_login(username, tool, request.form.get('salt'), request.form.get('sign'))
    return _do_login(authenticated, username, tool)


@jsonrpc.route('/cas', methods=['GET'])
def cas():
    '''
    This is a proxy to the cas p3/serviceValidate endpoint which also sets the
    cookie on successful authentication.
    '''
    service = request.args.get('service')
    ticket = request.args.get('ticket')
    r = requests.get(urlparse.urljoin(current_app.config['CAS_URL'], 'p3/serviceValidate'),
                     params=dict(service=service, ticket=ticket))
    if r.status_code == 200:
        xml = et.fromstring(r.content)
        success = xml.find('{http://www.yale.edu/tp/cas}authenticationSuccess')
        if success:
            username = success.find('{http://www.yale.edu/tp/cas}user').text
            _do_login(True, username, tool=request.args.get('tool'))
    return Response(response=r.content, status=r.status_code, mimetype=r.headers['Content-Type'])


@jsonrpc.route('/NetdotLogin', methods=['POST'])
def old_login():
    authenticated = False
    username = request.form.get('credential_0')
    frontend_login = 'credential_2' in request.form
    tool = None
    if not frontend_login:
        authenticated = _check_credentials(username, request.form.get('credential_1'))
    else:
        tool = 'dimfe'
        salt = request.form.get('credential_2')
        sign = request.form.get('credential_3')
        authenticated = _check_tool_login(username, tool, salt, sign)
    return _do_login(authenticated, username, tool=tool)


@jsonrpc.route('/')
@jsonrpc.route('/index.html')
def index():
    if 'username' in session:
        return 'Hi there'
    else:
        abort(403)


@jsonrpc.route('/jsonrpc', methods=['POST'])
def jsonrpc_handler():
    if 'username' not in session:
        abort(403)
    # logging.debug('jsonrpc request: %r', request.data)
    json_response = dict(jsonrpc='2.0', id=None)
    try:
        json_request = json.loads(request.data)
    except Exception as e:
        return jsonify(error=dict(code=-32700, message='Parse error', data=text_type(e)),
                       **json_response)

    json_response['id'] = json_request.get('id', None)
    rpc = TRPC(username=session['username'], tool=session.get('tool', None), ip=request.remote_addr)

    method_name = json_request.get('method', None)
    if method_name is None or method_name.startswith('_'):
        method = None
    else:
        method = getattr(rpc, method_name, None)
    if method is None:
        return jsonify(error=dict(code=-32601, message='Method not found'), **json_response)

    try:
        params = json_request.get('params', [])
        args, kwargs = _expand_jsonrpc_options(method, params)
        bigint_as_string = kwargs.pop('bigint_as_string', False)
        return json.dumps(dict(result=method(*args, **kwargs), **json_response),
                          default=default_for_json,
                          bigint_as_string=bigint_as_string)
    except DimError as e:
        logging.info(u'DimError: %s', e)
        return jsonify(error=dict(code=e.code, message=text_type(e)), **json_response)
    except Exception as e:
        logging.exception(e)
        return jsonify(error=dict(code=1, message=text_type(e)), **json_response)


def default_for_json(o):
    if isinstance(o, datetime.datetime):
        return o.strftime('%Y-%m-%d %H:%M:%S.%f')


def _expand_jsonrpc_options(method, params):
    if method.__name__ in _options_pos:
        pos = _options_pos[method.__name__]
        if pos is not None and len(params) == pos:
            args = params[:-1]
            kwargs = params[-1]
            if not isinstance(kwargs, dict):
                raise Exception('options must be an object')
            return args, kwargs
    return params, {}
