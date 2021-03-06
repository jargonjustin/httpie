"""Persistent, JSON-serialized sessions.

"""
import re
import os
import sys
import glob
import errno
import codecs
import shutil
import subprocess

import requests
from requests.compat import urlparse
from requests.cookies import RequestsCookieJar, create_cookie
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

from .config import BaseConfigDict, DEFAULT_CONFIG_DIR
from .output import PygmentsProcessor


SESSIONS_DIR_NAME = 'sessions'
DEFAULT_SESSIONS_DIR = os.path.join(DEFAULT_CONFIG_DIR, SESSIONS_DIR_NAME)


def get_response(name, request_kwargs, config_dir, read_only=False):
    """Like `client.get_response`, but applies permanent
    aspects of the session to the request.

    """
    sessions_dir = os.path.join(config_dir, SESSIONS_DIR_NAME)
    host = Host(
        root_dir=sessions_dir,
        name=request_kwargs['headers'].get('Host', None)
             or urlparse(request_kwargs['url']).netloc.split('@')[-1]
    )

    session = Session(host, name)
    session.load()

    # Update session headers with the request headers.
    session['headers'].update(request_kwargs.get('headers', {}))
    # Use the merged headers for the request
    request_kwargs['headers'] = session['headers']

    auth = request_kwargs.get('auth', None)
    if auth:
        session.auth = auth
    elif session.auth:
        request_kwargs['auth'] = session.auth

    rsession = requests.Session()
    try:
        response = rsession.request(cookies=session.cookies, **request_kwargs)
    except Exception:
        raise
    else:
        # Existing sessions with `read_only=True` don't get updated.
        if session.is_new or not read_only:
            session.cookies = rsession.cookies
            session.save()
        return response


class Host(object):
    """A host is a per-host directory on the disk containing sessions files."""

    VALID_NAME_PATTERN = re.compile('^[a-zA-Z0-9_.:-]+$')

    def __init__(self, name, root_dir=DEFAULT_SESSIONS_DIR):
        assert self.VALID_NAME_PATTERN.match(name)
        self.name = name
        self.root_dir = root_dir

    def __iter__(self):
        """Return an iterator yielding `Session` instances."""
        for fn in sorted(glob.glob1(self.path, '*.json')):
            session_name = os.path.splitext(fn)[0]
            yield Session(host=self, name=session_name)

    @property
    def verbose_name(self):
        return '%s %s' % (self.name, self.path)

    def delete(self):
        shutil.rmtree(self.path)

    @property
    def path(self):
        # Name will include ':' if a port is specified, which is invalid
        # on windows. DNS does not allow '_' in a domain, or for it to end
        # in a number (I think?)
        path = os.path.join(self.root_dir, self.name.replace(':', '_'))
        try:
            os.makedirs(path, mode=0o700)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise
        return path

    @classmethod
    def all(cls, root_dir=DEFAULT_SESSIONS_DIR):
        """Return a generator yielding a host at a time."""
        for name in sorted(glob.glob1(root_dir, '*')):
            if os.path.isdir(os.path.join(root_dir, name)):
                # host_port => host:port
                real_name = re.sub(r'_(\d+)$', r':\1', name)
                yield Host(real_name, root_dir=root_dir)


class Session(BaseConfigDict):
    """"""

    help = 'https://github.com/jkbr/httpie#sessions'
    about = 'HTTPie session file'

    VALID_NAME_PATTERN = re.compile('^[a-zA-Z0-9_.-]+$')

    def __init__(self, host, name, *args, **kwargs):
        assert self.VALID_NAME_PATTERN.match(name)
        super(Session, self).__init__(*args, **kwargs)
        self.host = host
        self.name = name
        self['headers'] = {}
        self['cookies'] = {}
        self['auth'] = {
            'type': None,
            'username': None,
            'password': None
        }

    @property
    def directory(self):
        return self.host.path

    @property
    def verbose_name(self):
        return '%s %s %s' % (self.host.name, self.name, self.path)

    @property
    def cookies(self):
        jar = RequestsCookieJar()
        for name, cookie_dict in self['cookies'].items():
            jar.set_cookie(create_cookie(
                name, cookie_dict.pop('value'), **cookie_dict))
        jar.clear_expired_cookies()
        return jar

    @cookies.setter
    def cookies(self, jar):
        excluded = [
            '_rest', 'name', 'port_specified',
            'domain_specified', 'domain_initial_dot',
            'path_specified', 'comment', 'comment_url'
        ]
        self['cookies'] = {}
        for host in jar._cookies.values():
            for path in host.values():
                for name, cookie in path.items():
                    cookie_dict = {}
                    for k, v in cookie.__dict__.items():
                        if k not in excluded:
                            cookie_dict[k] = v
                    self['cookies'][name] = cookie_dict

    @property
    def auth(self):
        auth = self.get('auth', None)
        if not auth or not auth['type']:
            return
        Auth = {'basic': HTTPBasicAuth,
                'digest': HTTPDigestAuth}[auth['type']]
        return Auth(auth['username'], auth['password'])

    @auth.setter
    def auth(self, cred):
        self['auth'] = {
            'type': {HTTPBasicAuth: 'basic',
                     HTTPDigestAuth: 'digest'}[type(cred)],
            'username': cred.username,
            'password': cred.password,
        }


##################################################################
# Session management commands
# TODO: write tests
##################################################################


def command_session_list(hostname=None):
    """Print a list of all sessions or only
    the ones from `args.host`, if provided.

    """
    if hostname:
        for session in Host(hostname):
            print(session.verbose_name)
    else:
        for host in Host.all():
            for session in host:
                print(session.verbose_name)


def command_session_show(hostname, session_name):
    """Print JSON data for a session."""
    session = Session(Host(hostname), session_name)
    path = session.path
    if not os.path.exists(path):
        sys.stderr.write('Session does not exist: %s\n'
                         % session.verbose_name)
        sys.exit(1)

    with codecs.open(path, encoding='utf8') as f:
        print(session.verbose_name + ':\n')
        proc = PygmentsProcessor()
        print(proc.process_body(f.read(), 'application/json', 'json'))
        print('')


def command_session_delete(hostname, session_name=None):
    """Delete a session by host and name, or delete all the
    host's session if name not provided.

    """
    host = Host(hostname)
    if not session_name:
        host.delete()
        session = Session(host, session_name)
        session.delete()


def command_session_edit(hostname, session_name):
    """Open a session file in EDITOR."""
    editor = os.environ.get('EDITOR', None)
    if not editor:
        sys.stderr.write(
            'You need to configure the environment variable EDITOR.\n')
        sys.exit(1)

    session = Session(Host(hostname), session_name)
    if session.is_new:
        session.save()

    command = editor.split()
    command.append(session.path)
    subprocess.call(command)
