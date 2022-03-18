import time
from io import BufferedReader
from logging import getLogger as get_logger
from typing import Iterable
from urllib.parse import urlsplit

import requests

from megfile.errors import http_should_retry, patch_method, translate_http_error
from megfile.interfaces import PathLike, StatResult
from megfile.lib.compat import fspath
from megfile.utils import binary_open

__all__ = [
    'is_http',
    'http_open',
    'http_getsize',
    'http_getmtime',
    'http_stat',
]

_logger = get_logger(__name__)

max_retries = 10


def get_http_session(
        timeout: int = 10, status_forcelist: Iterable[int] = (502, 503, 504)):
    session = requests.Session()
    session.timeout = timeout

    def after_callback(response, *args, **kwargs):
        if response.status_code in status_forcelist:
            response.raise_for_status()
        return response

    def before_callback(method, url, **kwargs):
        _logger.debug(  # pragma: no cover
            'send http request: %s %r, with parameters: %s', method, url,
            kwargs)

    session.request = patch_method(
        session.request,
        max_retries=max_retries,
        should_retry=http_should_retry,
        before_callback=before_callback,
        after_callback=after_callback,
    )
    return session


def is_http(path: PathLike) -> bool:
    '''http scheme definition: http(s)://domain/path

    :param path: Path to be tested
    :returns: True if path is http url, else False
    '''

    path = fspath(path)
    if not isinstance(path, str) or not (path.startswith('http://') or
                                         path.startswith('https://')):
        return False

    parts = urlsplit(path)
    return parts.scheme == 'http' or parts.scheme == 'https'


@binary_open
def http_open(path: PathLike, mode: str = 'rb') -> BufferedReader:
    '''Open a BytesIO to read binary data of given http(s) url

    .. note ::

        Essentially, it reads data of http(s) url to memory by requests, and then return BytesIO to user.

    :param path: http(s) url, e.g.: http(s)://domain/path
    :param mode: Only supports 'rb' mode now
    :return: BytesIO initialized with http(s) data
    '''
    path = str(path)

    if mode not in ('rb',):
        raise ValueError('unacceptable mode: %r' % mode)

    try:
        response = requests.get(path, stream=True, timeout=10.0)
        response.raise_for_status()
    except Exception as error:
        raise translate_http_error(error, path)

    response.raw.auto_close = False
    return BufferedReader(response.raw)


def http_stat(path: PathLike) -> StatResult:
    '''
    Get StatResult of path response, including size and mtime, referring to http_getsize and http_getmtime

    :param path: Given http url
    :returns: StatResult
    :raises: HttpPermissionError, HttpFileNotFoundError
    '''
    path = str(path)
    try:
        response = requests.get(path, stream=True, timeout=10.0)
        response.raise_for_status()
    except Exception as error:
        raise translate_http_error(error, path)

    size = response.headers.get('Content-Length')
    if size:
        size = int(size)

    last_modified = response.headers.get('Last-Modified')
    if last_modified:
        last_modified = time.mktime(
            time.strptime(last_modified, "%a, %d %b %Y %H:%M:%S %Z"))

    return StatResult(  # pyre-ignore[20]
        size=size, mtime=last_modified, isdir=False,
        islnk=False, extra=response.headers)


def http_getsize(path: PathLike) -> int:
    '''
    Get file size on the given path path.

    If http response header don't support Content-Length, will return None

    :param path: Given http path
    :returns: File size (in bytes)
    :raises: HttpPermissionError, HttpFileNotFoundError
    '''
    path = str(path)
    return http_stat(path).size


def http_getmtime(path: PathLike) -> float:
    '''
    Get Last-Modified time of the http request on the given path path.
    
    If http response header don't support Last-Modified, will return None

    :param path: Given http url
    :returns: Last-Modified time (in Unix timestamp format)
    :raises: HttpPermissionError, HttpFileNotFoundError
    '''
    path = str(path)
    return http_stat(path).mtime
