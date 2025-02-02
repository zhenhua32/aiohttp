"""HTTP Client for asyncio."""

import asyncio
import base64
import hashlib
import json
import os
import sys
import traceback
import warnings
from types import SimpleNamespace, TracebackType
from typing import (  # noqa
    Any,
    Coroutine,
    Generator,
    Generic,
    Iterable,
    List,
    Mapping,
    Optional,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
)

import attr
from multidict import CIMultiDict, MultiDict, MultiDictProxy, istr
from yarl import URL

from . import hdrs, http, payload
from .abc import AbstractCookieJar
from .client_exceptions import (
    ClientConnectionError,
    ClientConnectorCertificateError,
    ClientConnectorError,
    ClientConnectorSSLError,
    ClientError,
    ClientHttpProxyError,
    ClientOSError,
    ClientPayloadError,
    ClientProxyConnectionError,
    ClientResponseError,
    ClientSSLError,
    ContentTypeError,
    InvalidURL,
    ServerConnectionError,
    ServerDisconnectedError,
    ServerFingerprintMismatch,
    ServerTimeoutError,
    TooManyRedirects,
    WSServerHandshakeError,
)
from .client_reqrep import (
    SSL_ALLOWED_TYPES,
    ClientRequest,
    ClientResponse,
    Fingerprint,
    RequestInfo,
)
from .client_ws import ClientWebSocketResponse
from .connector import (
    BaseConnector,
    NamedPipeConnector,
    TCPConnector,
    UnixConnector,
)
from .cookiejar import CookieJar
from .helpers import (
    PY_36,
    BasicAuth,
    CeilTimeout,
    TimeoutHandle,
    get_running_loop,
    proxies_from_env,
    sentinel,
    strip_auth_from_url,
)
from .http import WS_KEY, HttpVersion, WebSocketReader, WebSocketWriter
from .http_websocket import (  # noqa
    WSHandshakeError,
    WSMessage,
    ws_ext_gen,
    ws_ext_parse,
)
from .streams import FlowControlDataQueue
from .tracing import Trace, TraceConfig
from .typedefs import JSONEncoder, LooseCookies, LooseHeaders, StrOrURL

__all__ = (
    # client_exceptions
    'ClientConnectionError',
    'ClientConnectorCertificateError',
    'ClientConnectorError',
    'ClientConnectorSSLError',
    'ClientError',
    'ClientHttpProxyError',
    'ClientOSError',
    'ClientPayloadError',
    'ClientProxyConnectionError',
    'ClientResponseError',
    'ClientSSLError',
    'ContentTypeError',
    'InvalidURL',
    'ServerConnectionError',
    'ServerDisconnectedError',
    'ServerFingerprintMismatch',
    'ServerTimeoutError',
    'TooManyRedirects',
    'WSServerHandshakeError',
    # client_reqrep
    'ClientRequest',
    'ClientResponse',
    'Fingerprint',
    'RequestInfo',
    # connector
    'BaseConnector',
    'TCPConnector',
    'UnixConnector',
    'NamedPipeConnector',
    # client_ws
    'ClientWebSocketResponse',
    # client
    'ClientSession',
    'ClientTimeout',
    'request')


try:
    from ssl import SSLContext
except ImportError:  # pragma: no cover
    SSLContext = object  # type: ignore


@attr.s(frozen=True, slots=True)
class ClientTimeout:
    total = attr.ib(type=Optional[float], default=None)
    connect = attr.ib(type=Optional[float], default=None)
    sock_read = attr.ib(type=Optional[float], default=None)
    sock_connect = attr.ib(type=Optional[float], default=None)

    # pool_queue_timeout = attr.ib(type=float, default=None)
    # dns_resolution_timeout = attr.ib(type=float, default=None)
    # socket_connect_timeout = attr.ib(type=float, default=None)
    # connection_acquiring_timeout = attr.ib(type=float, default=None)
    # new_connection_timeout = attr.ib(type=float, default=None)
    # http_header_timeout = attr.ib(type=float, default=None)
    # response_body_timeout = attr.ib(type=float, default=None)

    # to create a timeout specific for a single request, either
    # - create a completely new one to overwrite the default
    # - or use http://www.attrs.org/en/stable/api.html#attr.evolve
    # to overwrite the defaults


# 5 Minute default read timeout
DEFAULT_TIMEOUT = ClientTimeout(total=5*60)

_RetType = TypeVar('_RetType')


class ClientSession:
    """First-class interface for making HTTP requests."""

    __slots__ = (
        '_source_traceback', '_connector',
        '_loop', '_cookie_jar',
        '_connector_owner', '_default_auth',
        '_version', '_json_serialize',
        '_requote_redirect_url',
        '_timeout', '_raise_for_status', '_auto_decompress',
        '_trust_env', '_default_headers', '_skip_auto_headers',
        '_request_class', '_response_class',
        '_ws_response_class', '_trace_configs')

    def __init__(self, *, connector: Optional[BaseConnector]=None,
                 loop: Optional[asyncio.AbstractEventLoop]=None,
                 cookies: Optional[LooseCookies]=None,
                 headers: Optional[LooseHeaders]=None,
                 skip_auto_headers: Optional[Iterable[str]]=None,
                 auth: Optional[BasicAuth]=None,
                 json_serialize: JSONEncoder=json.dumps,
                 request_class: Type[ClientRequest]=ClientRequest,
                 response_class: Type[ClientResponse]=ClientResponse,
                 ws_response_class: Type[ClientWebSocketResponse]=ClientWebSocketResponse,  # noqa
                 version: HttpVersion=http.HttpVersion11,
                 cookie_jar: Optional[AbstractCookieJar]=None,
                 connector_owner: bool=True,
                 raise_for_status: bool=False,
                 read_timeout: Union[float, object]=sentinel,
                 conn_timeout: Optional[float]=None,
                 timeout: Union[object, ClientTimeout]=sentinel,
                 auto_decompress: bool=True,
                 trust_env: bool=False,
                 requote_redirect_url: bool=True,
                 trace_configs: Optional[List[TraceConfig]]=None) -> None:

        if loop is None:
            if connector is not None:
                loop = connector._loop

        loop = get_running_loop(loop)

        if connector is None:
            connector = TCPConnector(loop=loop)

        # Initialize these three attrs before raising any exception,
        # they are used in __del__
        self._connector = connector  # type: Optional[BaseConnector]
        self._loop = loop
        if loop.get_debug():
            self._source_traceback = traceback.extract_stack(sys._getframe(1))  # type: Optional[traceback.StackSummary]  # noqa
        else:
            self._source_traceback = None

        if connector._loop is not loop:
            raise RuntimeError(
                "Session and connector have to use same event loop")

        if cookie_jar is None:
            cookie_jar = CookieJar(loop=loop)
        self._cookie_jar = cookie_jar

        if cookies is not None:
            self._cookie_jar.update_cookies(cookies)

        self._connector_owner = connector_owner
        self._default_auth = auth
        self._version = version
        self._json_serialize = json_serialize
        if timeout is sentinel:
            self._timeout = DEFAULT_TIMEOUT
            if read_timeout is not sentinel:
                warnings.warn("read_timeout is deprecated, "
                              "use timeout argument instead",
                              DeprecationWarning,
                              stacklevel=2)
                self._timeout = attr.evolve(self._timeout, total=read_timeout)
            if conn_timeout is not None:
                self._timeout = attr.evolve(self._timeout,
                                            connect=conn_timeout)
                warnings.warn("conn_timeout is deprecated, "
                              "use timeout argument instead",
                              DeprecationWarning,
                              stacklevel=2)
        else:
            self._timeout = timeout  # type: ignore
            if read_timeout is not sentinel:
                raise ValueError("read_timeout and timeout parameters "
                                 "conflict, please setup "
                                 "timeout.read")
            if conn_timeout is not None:
                raise ValueError("conn_timeout and timeout parameters "
                                 "conflict, please setup "
                                 "timeout.connect")
        self._raise_for_status = raise_for_status
        self._auto_decompress = auto_decompress
        self._trust_env = trust_env
        self._requote_redirect_url = requote_redirect_url

        # Convert to list of tuples
        if headers:
            headers = CIMultiDict(headers)
        else:
            headers = CIMultiDict()
        self._default_headers = headers
        if skip_auto_headers is not None:
            self._skip_auto_headers = frozenset([istr(i)
                                                 for i in skip_auto_headers])
        else:
            self._skip_auto_headers = frozenset()

        self._request_class = request_class
        self._response_class = response_class
        self._ws_response_class = ws_response_class

        self._trace_configs = trace_configs or []
        for trace_config in self._trace_configs:
            trace_config.freeze()

    def __init_subclass__(cls: Type['ClientSession']) -> None:
        warnings.warn("Inheritance class {} from ClientSession "
                      "is discouraged".format(cls.__name__),
                      DeprecationWarning,
                      stacklevel=2)

    def __del__(self, _warnings: Any=warnings) -> None:
        if not self.closed:
            if PY_36:
                kwargs = {'source': self}
            else:
                kwargs = {}
            _warnings.warn("Unclosed client session {!r}".format(self),
                           ResourceWarning,
                           **kwargs)
            context = {'client_session': self,
                       'message': 'Unclosed client session'}
            if self._source_traceback is not None:
                context['source_traceback'] = self._source_traceback
            self._loop.call_exception_handler(context)

    def request(self,
                method: str,
                url: StrOrURL,
                **kwargs: Any) -> '_RequestContextManager':
        """Perform HTTP request."""
        return _RequestContextManager(self._request(method, url, **kwargs))

    async def _request(
            self,
            method: str,
            str_or_url: StrOrURL, *,
            params: Optional[Mapping[str, str]]=None,
            data: Any=None,
            json: Any=None,
            cookies: Optional[LooseCookies]=None,
            headers: LooseHeaders=None,
            skip_auto_headers: Optional[Iterable[str]]=None,
            auth: Optional[BasicAuth]=None,
            allow_redirects: bool=True,
            max_redirects: int=10,
            compress: Optional[str]=None,
            chunked: Optional[bool]=None,
            expect100: bool=False,
            raise_for_status: Optional[bool]=None,
            read_until_eof: bool=True,
            proxy: Optional[StrOrURL]=None,
            proxy_auth: Optional[BasicAuth]=None,
            timeout: Union[ClientTimeout, object]=sentinel,
            ssl: Optional[Union[SSLContext, bool, Fingerprint]]=None,
            proxy_headers: Optional[LooseHeaders]=None,
            trace_request_ctx: Optional[SimpleNamespace]=None
    ) -> ClientResponse:

        # NOTE: timeout clamps existing connect and read timeouts.  We cannot
        # set the default to None because we need to detect if the user wants
        # to use the existing timeouts by setting timeout to None.

        if self.closed:
            raise RuntimeError('Session is closed')

        if not isinstance(ssl, SSL_ALLOWED_TYPES):
            raise TypeError("ssl should be SSLContext, bool, Fingerprint, "
                            "or None, got {!r} instead.".format(ssl))

        if data is not None and json is not None:
            raise ValueError(
                'data and json parameters can not be used at the same time')
        elif json is not None:
            data = payload.JsonPayload(json, dumps=self._json_serialize)

        if not isinstance(chunked, bool) and chunked is not None:
            warnings.warn(
                'Chunk size is deprecated #1615', DeprecationWarning)

        redirects = 0
        history = []
        version = self._version

        # Merge with default headers and transform to CIMultiDict
        headers = self._prepare_headers(headers)
        proxy_headers = self._prepare_headers(proxy_headers)

        try:
            url = URL(str_or_url)
        except ValueError:
            raise InvalidURL(str_or_url)

        skip_headers = set(self._skip_auto_headers)
        if skip_auto_headers is not None:
            for i in skip_auto_headers:
                skip_headers.add(istr(i))

        if proxy is not None:
            try:
                proxy = URL(proxy)
            except ValueError:
                raise InvalidURL(proxy)

        if timeout is sentinel:
            real_timeout = self._timeout  # type: ClientTimeout
        else:
            if not isinstance(timeout, ClientTimeout):
                real_timeout = ClientTimeout(total=timeout)  # type: ignore
            else:
                real_timeout = timeout
        # timeout is cumulative for all request operations
        # (request, redirects, responses, data consuming)
        tm = TimeoutHandle(self._loop, real_timeout.total)
        handle = tm.start()

        traces = [
            Trace(
                self,
                trace_config,
                trace_config.trace_config_ctx(
                    trace_request_ctx=trace_request_ctx)
            )
            for trace_config in self._trace_configs
        ]

        for trace in traces:
            await trace.send_request_start(
                method,
                url,
                headers
            )

        timer = tm.timer()
        try:
            with timer:
                while True:
                    url, auth_from_url = strip_auth_from_url(url)
                    if auth and auth_from_url:
                        raise ValueError("Cannot combine AUTH argument with "
                                         "credentials encoded in URL")

                    if auth is None:
                        auth = auth_from_url
                    if auth is None:
                        auth = self._default_auth
                    # It would be confusing if we support explicit
                    # Authorization header with auth argument
                    if (headers is not None and
                            auth is not None and
                            hdrs.AUTHORIZATION in headers):
                        raise ValueError("Cannot combine AUTHORIZATION header "
                                         "with AUTH argument or credentials "
                                         "encoded in URL")

                    all_cookies = self._cookie_jar.filter_cookies(url)

                    if cookies is not None:
                        tmp_cookie_jar = CookieJar()
                        tmp_cookie_jar.update_cookies(cookies)
                        req_cookies = tmp_cookie_jar.filter_cookies(url)
                        if req_cookies:
                            all_cookies.load(req_cookies)

                    if proxy is not None:
                        proxy = URL(proxy)
                    elif self._trust_env:
                        for scheme, proxy_info in proxies_from_env().items():
                            if scheme == url.scheme:
                                proxy = proxy_info.proxy
                                proxy_auth = proxy_info.proxy_auth
                                break

                    req = self._request_class(
                        method, url, params=params, headers=headers,
                        skip_auto_headers=skip_headers, data=data,
                        cookies=all_cookies, auth=auth, version=version,
                        compress=compress, chunked=chunked,
                        expect100=expect100, loop=self._loop,
                        response_class=self._response_class,
                        proxy=proxy, proxy_auth=proxy_auth, timer=timer,
                        session=self,
                        ssl=ssl, proxy_headers=proxy_headers, traces=traces)

                    # connection timeout
                    try:
                        with CeilTimeout(real_timeout.connect,
                                         loop=self._loop):
                            assert self._connector is not None
                            conn = await self._connector.connect(
                                req,
                                traces=traces,
                                timeout=real_timeout
                            )
                    except asyncio.TimeoutError as exc:
                        raise ServerTimeoutError(
                            'Connection timeout '
                            'to host {0}'.format(url)) from exc

                    assert conn.transport is not None

                    assert conn.protocol is not None
                    conn.protocol.set_response_params(
                        timer=timer,
                        skip_payload=method.upper() == 'HEAD',
                        read_until_eof=read_until_eof,
                        auto_decompress=self._auto_decompress,
                        read_timeout=real_timeout.sock_read)

                    try:
                        try:
                            resp = await req.send(conn)
                            try:
                                await resp.start(conn)
                            except BaseException:
                                resp.close()
                                raise
                        except BaseException:
                            conn.close()
                            raise
                    except ClientError:
                        raise
                    except OSError as exc:
                        raise ClientOSError(*exc.args) from exc

                    self._cookie_jar.update_cookies(resp.cookies, resp.url)

                    # redirects
                    if resp.status in (
                            301, 302, 303, 307, 308) and allow_redirects:

                        for trace in traces:
                            await trace.send_request_redirect(
                                method,
                                url,
                                headers,
                                resp
                            )

                        redirects += 1
                        history.append(resp)
                        if max_redirects and redirects >= max_redirects:
                            resp.close()
                            raise TooManyRedirects(
                                history[0].request_info, tuple(history))

                        # For 301 and 302, mimic IE, now changed in RFC
                        # https://github.com/kennethreitz/requests/pull/269
                        if (resp.status == 303 and
                                resp.method != hdrs.METH_HEAD) \
                                or (resp.status in (301, 302) and
                                    resp.method == hdrs.METH_POST):
                            method = hdrs.METH_GET
                            data = None
                            if headers.get(hdrs.CONTENT_LENGTH):
                                headers.pop(hdrs.CONTENT_LENGTH)

                        r_url = (resp.headers.get(hdrs.LOCATION) or
                                 resp.headers.get(hdrs.URI))
                        if r_url is None:
                            # see github.com/aio-libs/aiohttp/issues/2022
                            break
                        else:
                            # reading from correct redirection
                            # response is forbidden
                            resp.release()

                        try:
                            r_url = URL(
                                r_url, encoded=not self._requote_redirect_url)

                        except ValueError:
                            raise InvalidURL(r_url)

                        scheme = r_url.scheme
                        if scheme not in ('http', 'https', ''):
                            resp.close()
                            raise ValueError(
                                'Can redirect only to http or https')
                        elif not scheme:
                            r_url = url.join(r_url)

                        if url.origin() != r_url.origin():
                            auth = None
                            headers.pop(hdrs.AUTHORIZATION, None)

                        url = r_url
                        params = None
                        resp.release()
                        continue

                    break

            # check response status
            if raise_for_status is None:
                raise_for_status = self._raise_for_status
            if raise_for_status:
                resp.raise_for_status()

            # register connection
            if handle is not None:
                if resp.connection is not None:
                    resp.connection.add_callback(handle.cancel)
                else:
                    handle.cancel()

            resp._history = tuple(history)

            for trace in traces:
                await trace.send_request_end(
                    method,
                    url,
                    headers,
                    resp
                )
            return resp

        except BaseException as e:
            # cleanup timer
            tm.close()
            if handle:
                handle.cancel()
                handle = None

            for trace in traces:
                await trace.send_request_exception(
                    method,
                    url,
                    headers,
                    e
                )
            raise

    def ws_connect(
            self,
            url: StrOrURL, *,
            method: str=hdrs.METH_GET,
            protocols: Iterable[str]=(),
            timeout: float=10.0,
            receive_timeout: Optional[float]=None,
            autoclose: bool=True,
            autoping: bool=True,
            heartbeat: Optional[float]=None,
            auth: Optional[BasicAuth]=None,
            origin: Optional[str]=None,
            headers: Optional[LooseHeaders]=None,
            proxy: Optional[StrOrURL]=None,
            proxy_auth: Optional[BasicAuth]=None,
            ssl: Union[SSLContext, bool, None, Fingerprint]=None,
            proxy_headers: Optional[LooseHeaders]=None,
            compress: int=0,
            max_msg_size: int=4*1024*1024) -> '_WSRequestContextManager':
        """Initiate websocket connection."""
        return _WSRequestContextManager(
            self._ws_connect(url,
                             method=method,
                             protocols=protocols,
                             timeout=timeout,
                             receive_timeout=receive_timeout,
                             autoclose=autoclose,
                             autoping=autoping,
                             heartbeat=heartbeat,
                             auth=auth,
                             origin=origin,
                             headers=headers,
                             proxy=proxy,
                             proxy_auth=proxy_auth,
                             ssl=ssl,
                             proxy_headers=proxy_headers,
                             compress=compress,
                             max_msg_size=max_msg_size))

    async def _ws_connect(
            self,
            url: StrOrURL, *,
            method: str=hdrs.METH_GET,
            protocols: Iterable[str]=(),
            timeout: float=10.0,
            receive_timeout: Optional[float]=None,
            autoclose: bool=True,
            autoping: bool=True,
            heartbeat: Optional[float]=None,
            auth: Optional[BasicAuth]=None,
            origin: Optional[str]=None,
            headers: Optional[LooseHeaders]=None,
            proxy: Optional[StrOrURL]=None,
            proxy_auth: Optional[BasicAuth]=None,
            ssl: Union[SSLContext, bool, None, Fingerprint]=None,
            proxy_headers: Optional[LooseHeaders]=None,
            compress: int=0,
            max_msg_size: int=4*1024*1024
    ) -> ClientWebSocketResponse:

        if headers is None:
            real_headers = CIMultiDict()  # type: CIMultiDict[str]
        else:
            real_headers = CIMultiDict(headers)

        default_headers = {
            hdrs.UPGRADE: hdrs.WEBSOCKET,
            hdrs.CONNECTION: hdrs.UPGRADE,
            hdrs.SEC_WEBSOCKET_VERSION: '13',
        }

        for key, value in default_headers.items():
            real_headers.setdefault(key, value)

        sec_key = base64.b64encode(os.urandom(16))
        real_headers[hdrs.SEC_WEBSOCKET_KEY] = sec_key.decode()

        if protocols:
            real_headers[hdrs.SEC_WEBSOCKET_PROTOCOL] = ','.join(protocols)
        if origin is not None:
            real_headers[hdrs.ORIGIN] = origin
        if compress:
            extstr = ws_ext_gen(compress=compress)
            real_headers[hdrs.SEC_WEBSOCKET_EXTENSIONS] = extstr

        if not isinstance(ssl, SSL_ALLOWED_TYPES):
            raise TypeError("ssl should be SSLContext, bool, Fingerprint, "
                            "or None, got {!r} instead.".format(ssl))

        # send request
        resp = await self.request(method, url,
                                  headers=real_headers,
                                  read_until_eof=False,
                                  auth=auth,
                                  proxy=proxy,
                                  proxy_auth=proxy_auth,
                                  ssl=ssl,
                                  proxy_headers=proxy_headers)

        try:
            # check handshake
            if resp.status != 101:
                raise WSServerHandshakeError(
                    resp.request_info,
                    resp.history,
                    message='Invalid response status',
                    status=resp.status,
                    headers=resp.headers)

            if resp.headers.get(hdrs.UPGRADE, '').lower() != 'websocket':
                raise WSServerHandshakeError(
                    resp.request_info,
                    resp.history,
                    message='Invalid upgrade header',
                    status=resp.status,
                    headers=resp.headers)

            if resp.headers.get(hdrs.CONNECTION, '').lower() != 'upgrade':
                raise WSServerHandshakeError(
                    resp.request_info,
                    resp.history,
                    message='Invalid connection header',
                    status=resp.status,
                    headers=resp.headers)

            # key calculation
            key = resp.headers.get(hdrs.SEC_WEBSOCKET_ACCEPT, '')
            match = base64.b64encode(
                hashlib.sha1(sec_key + WS_KEY).digest()).decode()
            if key != match:
                raise WSServerHandshakeError(
                    resp.request_info,
                    resp.history,
                    message='Invalid challenge response',
                    status=resp.status,
                    headers=resp.headers)

            # websocket protocol
            protocol = None
            if protocols and hdrs.SEC_WEBSOCKET_PROTOCOL in resp.headers:
                resp_protocols = [
                    proto.strip() for proto in
                    resp.headers[hdrs.SEC_WEBSOCKET_PROTOCOL].split(',')]

                for proto in resp_protocols:
                    if proto in protocols:
                        protocol = proto
                        break

            # websocket compress
            notakeover = False
            if compress:
                compress_hdrs = resp.headers.get(hdrs.SEC_WEBSOCKET_EXTENSIONS)
                if compress_hdrs:
                    try:
                        compress, notakeover = ws_ext_parse(compress_hdrs)
                    except WSHandshakeError as exc:
                        raise WSServerHandshakeError(
                            resp.request_info,
                            resp.history,
                            message=exc.args[0],
                            status=resp.status,
                            headers=resp.headers)
                else:
                    compress = 0
                    notakeover = False

            conn = resp.connection
            assert conn is not None
            proto = conn.protocol
            assert proto is not None
            transport = conn.transport
            assert transport is not None
            reader = FlowControlDataQueue(
                proto, limit=2 ** 16, loop=self._loop)  # type: FlowControlDataQueue[WSMessage]  # noqa
            proto.set_parser(WebSocketReader(reader, max_msg_size), reader)
            writer = WebSocketWriter(
                proto, transport, use_mask=True,
                compress=compress, notakeover=notakeover)
        except BaseException:
            resp.close()
            raise
        else:
            return self._ws_response_class(reader,
                                           writer,
                                           protocol,
                                           resp,
                                           timeout,
                                           autoclose,
                                           autoping,
                                           self._loop,
                                           receive_timeout=receive_timeout,
                                           heartbeat=heartbeat,
                                           compress=compress,
                                           client_notakeover=notakeover)

    def _prepare_headers(
            self,
            headers: Optional[LooseHeaders]) -> 'CIMultiDict[str]':
        """ Add default headers and transform it to CIMultiDict
        """
        # Convert headers to MultiDict
        result = CIMultiDict(self._default_headers)
        if headers:
            if not isinstance(headers, (MultiDictProxy, MultiDict)):
                headers = CIMultiDict(headers)
            added_names = set()  # type: Set[str]
            for key, value in headers.items():
                if key in added_names:
                    result.add(key, value)
                else:
                    result[key] = value
                    added_names.add(key)
        return result

    def get(self, url: StrOrURL, *, allow_redirects: bool=True,
            **kwargs: Any) -> '_RequestContextManager':
        """Perform HTTP GET request."""
        return _RequestContextManager(
            self._request(hdrs.METH_GET, url,
                          allow_redirects=allow_redirects,
                          **kwargs))

    def options(self, url: StrOrURL, *, allow_redirects: bool=True,
                **kwargs: Any) -> '_RequestContextManager':
        """Perform HTTP OPTIONS request."""
        return _RequestContextManager(
            self._request(hdrs.METH_OPTIONS, url,
                          allow_redirects=allow_redirects,
                          **kwargs))

    def head(self, url: StrOrURL, *, allow_redirects: bool=False,
             **kwargs: Any) -> '_RequestContextManager':
        """Perform HTTP HEAD request."""
        return _RequestContextManager(
            self._request(hdrs.METH_HEAD, url,
                          allow_redirects=allow_redirects,
                          **kwargs))

    def post(self, url: StrOrURL,
             *, data: Any=None, **kwargs: Any) -> '_RequestContextManager':
        """Perform HTTP POST request."""
        return _RequestContextManager(
            self._request(hdrs.METH_POST, url,
                          data=data,
                          **kwargs))

    def put(self, url: StrOrURL,
            *, data: Any=None, **kwargs: Any) -> '_RequestContextManager':
        """Perform HTTP PUT request."""
        return _RequestContextManager(
            self._request(hdrs.METH_PUT, url,
                          data=data,
                          **kwargs))

    def patch(self, url: StrOrURL,
              *, data: Any=None, **kwargs: Any) -> '_RequestContextManager':
        """Perform HTTP PATCH request."""
        return _RequestContextManager(
            self._request(hdrs.METH_PATCH, url,
                          data=data,
                          **kwargs))

    def delete(self, url: StrOrURL, **kwargs: Any) -> '_RequestContextManager':
        """Perform HTTP DELETE request."""
        return _RequestContextManager(
            self._request(hdrs.METH_DELETE, url,
                          **kwargs))

    async def close(self) -> None:
        """Close underlying connector.

        Release all acquired resources.
        """
        if not self.closed:
            if self._connector is not None and self._connector_owner:
                await self._connector.close()
            self._connector = None

    @property
    def closed(self) -> bool:
        """Is client session closed.

        A readonly property.
        """
        return self._connector is None or self._connector.closed

    @property
    def connector(self) -> Optional[BaseConnector]:
        """Connector instance used for the session."""
        return self._connector

    @property
    def cookie_jar(self) -> AbstractCookieJar:
        """The session cookies."""
        return self._cookie_jar

    @property
    def version(self) -> Tuple[int, int]:
        """The session HTTP protocol version."""
        return self._version

    @property
    def requote_redirect_url(self) -> bool:
        """Do URL requoting on redirection handling."""
        return self._requote_redirect_url

    @requote_redirect_url.setter
    def requote_redirect_url(self, val: bool) -> None:
        """Do URL requoting on redirection handling."""
        warnings.warn("session.requote_redirect_url modification "
                      "is deprecated #2778",
                      DeprecationWarning,
                      stacklevel=2)
        self._requote_redirect_url = val

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """Session's loop."""
        warnings.warn("client.loop property is deprecated",
                      DeprecationWarning,
                      stacklevel=2)
        return self._loop

    def detach(self) -> None:
        """Detach connector from session without closing the former.

        Session is switched to closed state anyway.
        """
        self._connector = None

    def __enter__(self) -> None:
        raise TypeError("Use async with instead")

    def __exit__(self,
                 exc_type: Optional[Type[BaseException]],
                 exc_val: Optional[BaseException],
                 exc_tb: Optional[TracebackType]) -> None:
        # __exit__ should exist in pair with __enter__ but never executed
        pass  # pragma: no cover

    async def __aenter__(self) -> 'ClientSession':
        return self

    async def __aexit__(self,
                        exc_type: Optional[Type[BaseException]],
                        exc_val: Optional[BaseException],
                        exc_tb: Optional[TracebackType]) -> None:
        await self.close()


class _BaseRequestContextManager(Coroutine[Any,
                                           Any,
                                           _RetType],
                                 Generic[_RetType]):

    __slots__ = ('_coro', '_resp')

    def __init__(
            self,
            coro: Coroutine['asyncio.Future[Any]', None, _RetType]
    ) -> None:
        self._coro = coro

    def send(self, arg: None) -> 'asyncio.Future[Any]':
        return self._coro.send(arg)

    def throw(self, arg: BaseException) -> None:  # type: ignore
        self._coro.throw(arg)  # type: ignore

    def close(self) -> None:
        return self._coro.close()

    def __await__(self) -> Generator[Any, None, _RetType]:
        ret = self._coro.__await__()
        return ret

    def __iter__(self) -> Generator[Any, None, _RetType]:
        return self.__await__()

    async def __aenter__(self) -> _RetType:
        self._resp = await self._coro
        return self._resp


class _RequestContextManager(_BaseRequestContextManager[ClientResponse]):
    async def __aexit__(self,
                        exc_type: Optional[Type[BaseException]],
                        exc: Optional[BaseException],
                        tb: Optional[TracebackType]) -> None:
        # We're basing behavior on the exception as it can be caused by
        # user code unrelated to the status of the connection.  If you
        # would like to close a connection you must do that
        # explicitly.  Otherwise connection error handling should kick in
        # and close/recycle the connection as required.
        self._resp.release()


class _WSRequestContextManager(_BaseRequestContextManager[
        ClientWebSocketResponse]):
    async def __aexit__(self,
                        exc_type: Optional[Type[BaseException]],
                        exc: Optional[BaseException],
                        tb: Optional[TracebackType]) -> None:
        await self._resp.close()


class _SessionRequestContextManager:

    __slots__ = ('_coro', '_resp', '_session')

    def __init__(self,
                 coro: Coroutine['asyncio.Future[Any]', None, ClientResponse],
                 session: ClientSession) -> None:
        self._coro = coro
        self._resp = None  # type: Optional[ClientResponse]
        self._session = session

    async def __aenter__(self) -> ClientResponse:
        self._resp = await self._coro
        return self._resp

    async def __aexit__(self,
                        exc_type: Optional[Type[BaseException]],
                        exc: Optional[BaseException],
                        tb: Optional[TracebackType]) -> None:
        assert self._resp is not None
        self._resp.close()
        await self._session.close()


def request(
        method: str,
        url: StrOrURL, *,
        params: Optional[Mapping[str, str]]=None,
        data: Any=None,
        json: Any=None,
        headers: LooseHeaders=None,
        skip_auto_headers: Optional[Iterable[str]]=None,
        auth: Optional[BasicAuth]=None,
        allow_redirects: bool=True,
        max_redirects: int=10,
        compress: Optional[str]=None,
        chunked: Optional[bool]=None,
        expect100: bool=False,
        raise_for_status: Optional[bool]=None,
        read_until_eof: bool=True,
        proxy: Optional[StrOrURL]=None,
        proxy_auth: Optional[BasicAuth]=None,
        timeout: Union[ClientTimeout, object]=sentinel,
        cookies: Optional[LooseCookies]=None,
        version: HttpVersion=http.HttpVersion11,
        connector: Optional[BaseConnector]=None,
        loop: Optional[asyncio.AbstractEventLoop]=None
) -> _SessionRequestContextManager:
    """Constructs and sends a request. Returns response object.
    method - HTTP method
    url - request url
    params - (optional) Dictionary or bytes to be sent in the query
      string of the new request
    data - (optional) Dictionary, bytes, or file-like object to
      send in the body of the request
    json - (optional) Any json compatible python object
    headers - (optional) Dictionary of HTTP Headers to send with
      the request
    cookies - (optional) Dict object to send with the request
    auth - (optional) BasicAuth named tuple represent HTTP Basic Auth
    auth - aiohttp.helpers.BasicAuth
    allow_redirects - (optional) If set to False, do not follow
      redirects
    version - Request HTTP version.
    compress - Set to True if request has to be compressed
       with deflate encoding.
    chunked - Set to chunk size for chunked transfer encoding.
    expect100 - Expect 100-continue response from server.
    connector - BaseConnector sub-class instance to support
       connection pooling.
    read_until_eof - Read response until eof if response
       does not have Content-Length header.
    loop - Optional event loop.
    timeout - Optional ClientTimeout settings structure, 5min
       total timeout by default.
    Usage::
      >>> import aiohttp
      >>> resp = await aiohttp.request('GET', 'http://python.org/')
      >>> resp
      <ClientResponse(python.org/) [200]>
      >>> data = await resp.read()
    """
    connector_owner = False
    if connector is None:
        connector_owner = True
        connector = TCPConnector(loop=loop, force_close=True)

    session = ClientSession(
        loop=loop, cookies=cookies, version=version, timeout=timeout,
        connector=connector, connector_owner=connector_owner)

    return _SessionRequestContextManager(
        session._request(method, url,
                         params=params,
                         data=data,
                         json=json,
                         headers=headers,
                         skip_auto_headers=skip_auto_headers,
                         auth=auth,
                         allow_redirects=allow_redirects,
                         max_redirects=max_redirects,
                         compress=compress,
                         chunked=chunked,
                         expect100=expect100,
                         raise_for_status=raise_for_status,
                         read_until_eof=read_until_eof,
                         proxy=proxy,
                         proxy_auth=proxy_auth,),
        session)
