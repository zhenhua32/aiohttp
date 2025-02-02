import asyncio
import contextlib
import gc
import json
import re
from http.cookies import SimpleCookie
from io import BytesIO
from unittest import mock

import pytest
from multidict import CIMultiDict, MultiDict
from yarl import URL

import aiohttp
from aiohttp import client, hdrs, web
from aiohttp.client import ClientSession
from aiohttp.client_reqrep import ClientRequest
from aiohttp.connector import BaseConnector, TCPConnector
from aiohttp.helpers import PY_36


@pytest.fixture
def connector(loop, create_mocked_conn):
    async def make_conn():
        return BaseConnector(loop=loop)
    conn = loop.run_until_complete(make_conn())
    proto = create_mocked_conn()
    conn._conns['a'] = [(proto, 123)]
    yield conn
    conn.close()


@pytest.fixture
def create_session(loop):
    session = None

    async def maker(*args, **kwargs):
        nonlocal session
        session = ClientSession(*args, loop=loop, **kwargs)
        return session
    yield maker
    if session is not None:
        loop.run_until_complete(session.close())


@pytest.fixture
def session(create_session, loop):
    return loop.run_until_complete(create_session())


@pytest.fixture
def params():
    return dict(
        headers={"Authorization": "Basic ..."},
        max_redirects=2,
        encoding="latin1",
        version=aiohttp.HttpVersion10,
        compress="deflate",
        chunked=True,
        expect100=True,
        read_until_eof=False)


async def test_close_coro(create_session) -> None:
    session = await create_session()
    await session.close()


async def test_init_headers_simple_dict(create_session) -> None:
    session = await create_session(headers={"h1": "header1",
                                            "h2": "header2"})
    assert (sorted(session._default_headers.items()) ==
            ([("h1", "header1"), ("h2", "header2")]))


async def test_init_headers_list_of_tuples(create_session) -> None:
    session = await create_session(headers=[("h1", "header1"),
                                            ("h2", "header2"),
                                            ("h3", "header3")])
    assert (session._default_headers ==
            CIMultiDict([("h1", "header1"),
                         ("h2", "header2"),
                         ("h3", "header3")]))


async def test_init_headers_MultiDict(create_session) -> None:
    session = await create_session(headers=MultiDict([("h1", "header1"),
                                                      ("h2", "header2"),
                                                      ("h3", "header3")]))
    assert (session._default_headers ==
            CIMultiDict([("H1", "header1"),
                         ("H2", "header2"),
                         ("H3", "header3")]))


async def test_init_headers_list_of_tuples_with_duplicates(
        create_session) -> None:
    session = await create_session(headers=[("h1", "header11"),
                                            ("h2", "header21"),
                                            ("h1", "header12")])
    assert (session._default_headers ==
            CIMultiDict([("H1", "header11"),
                         ("H2", "header21"),
                         ("H1", "header12")]))


async def test_init_cookies_with_simple_dict(create_session) -> None:
    session = await create_session(cookies={"c1": "cookie1",
                                            "c2": "cookie2"})
    cookies = session.cookie_jar.filter_cookies()
    assert set(cookies) == {'c1', 'c2'}
    assert cookies['c1'].value == 'cookie1'
    assert cookies['c2'].value == 'cookie2'


async def test_init_cookies_with_list_of_tuples(create_session) -> None:
    session = await create_session(cookies=[("c1", "cookie1"),
                                            ("c2", "cookie2")])

    cookies = session.cookie_jar.filter_cookies()
    assert set(cookies) == {'c1', 'c2'}
    assert cookies['c1'].value == 'cookie1'
    assert cookies['c2'].value == 'cookie2'


async def test_merge_headers(create_session) -> None:
    # Check incoming simple dict
    session = await create_session(headers={"h1": "header1",
                                            "h2": "header2"})
    headers = session._prepare_headers({"h1": "h1"})

    assert isinstance(headers, CIMultiDict)
    assert headers == {"h1": "h1", "h2": "header2"}


async def test_merge_headers_with_multi_dict(create_session) -> None:
    session = await create_session(headers={"h1": "header1",
                                            "h2": "header2"})
    headers = session._prepare_headers(MultiDict([("h1", "h1")]))
    assert isinstance(headers, CIMultiDict)
    assert headers == {"h1": "h1", "h2": "header2"}


async def test_merge_headers_with_list_of_tuples(create_session) -> None:
    session = await create_session(headers={"h1": "header1",
                                            "h2": "header2"})
    headers = session._prepare_headers([("h1", "h1")])
    assert isinstance(headers, CIMultiDict)
    assert headers == {"h1": "h1", "h2": "header2"}


async def test_merge_headers_with_list_of_tuples_duplicated_names(
        create_session) -> None:
    session = await create_session(headers={"h1": "header1",
                                            "h2": "header2"})

    headers = session._prepare_headers([("h1", "v1"),
                                        ("h1", "v2")])

    assert isinstance(headers, CIMultiDict)
    assert list(sorted(headers.items())) == [("h1", "v1"),
                                             ("h1", "v2"),
                                             ("h2", "header2")]


def test_http_GET(session, params) -> None:
    with mock.patch("aiohttp.client.ClientSession._request") as patched:
        session.get("http://test.example.com",
                    params={"x": 1},
                    **params)
    assert patched.called, "`ClientSession._request` not called"
    assert list(patched.call_args) == [("GET", "http://test.example.com",),
                                       dict(
                                           params={"x": 1},
                                           allow_redirects=True,
                                           **params)]


def test_http_OPTIONS(session, params) -> None:
    with mock.patch("aiohttp.client.ClientSession._request") as patched:
        session.options("http://opt.example.com",
                        params={"x": 2},
                        **params)
    assert patched.called, "`ClientSession._request` not called"
    assert list(patched.call_args) == [("OPTIONS", "http://opt.example.com",),
                                       dict(
                                           params={"x": 2},
                                           allow_redirects=True,
                                           **params)]


def test_http_HEAD(session, params) -> None:
    with mock.patch("aiohttp.client.ClientSession._request") as patched:
        session.head("http://head.example.com",
                     params={"x": 2},
                     **params)
    assert patched.called, "`ClientSession._request` not called"
    assert list(patched.call_args) == [("HEAD", "http://head.example.com",),
                                       dict(
                                           params={"x": 2},
                                           allow_redirects=False,
                                           **params)]


def test_http_POST(session, params) -> None:
    with mock.patch("aiohttp.client.ClientSession._request") as patched:
        session.post("http://post.example.com",
                     params={"x": 2},
                     data="Some_data",
                     **params)
    assert patched.called, "`ClientSession._request` not called"
    assert list(patched.call_args) == [("POST", "http://post.example.com",),
                                       dict(
                                           params={"x": 2},
                                           data="Some_data",
                                           **params)]


def test_http_PUT(session, params) -> None:
    with mock.patch("aiohttp.client.ClientSession._request") as patched:
        session.put("http://put.example.com",
                    params={"x": 2},
                    data="Some_data",
                    **params)
    assert patched.called, "`ClientSession._request` not called"
    assert list(patched.call_args) == [("PUT", "http://put.example.com",),
                                       dict(
                                           params={"x": 2},
                                           data="Some_data",
                                           **params)]


def test_http_PATCH(session, params) -> None:
    with mock.patch("aiohttp.client.ClientSession._request") as patched:
        session.patch("http://patch.example.com",
                      params={"x": 2},
                      data="Some_data",
                      **params)
    assert patched.called, "`ClientSession._request` not called"
    assert list(patched.call_args) == [("PATCH", "http://patch.example.com",),
                                       dict(
                                           params={"x": 2},
                                           data="Some_data",
                                           **params)]


def test_http_DELETE(session, params) -> None:
    with mock.patch("aiohttp.client.ClientSession._request") as patched:
        session.delete("http://delete.example.com",
                       params={"x": 2},
                       **params)
    assert patched.called, "`ClientSession._request` not called"
    assert list(patched.call_args) == [("DELETE",
                                        "http://delete.example.com",),
                                       dict(
                                           params={"x": 2},
                                           **params)]


async def test_close(create_session, connector) -> None:
    session = await create_session(connector=connector)

    await session.close()
    assert session.connector is None
    assert connector.closed


async def test_closed(session) -> None:
    assert not session.closed
    await session.close()
    assert session.closed


async def test_connector(create_session, loop, mocker) -> None:
    connector = TCPConnector(loop=loop)
    mocker.spy(connector, 'close')
    session = await create_session(connector=connector)
    assert session.connector is connector

    await session.close()
    assert connector.close.called
    connector.close()


async def test_create_connector(create_session, loop, mocker) -> None:
    session = await create_session()
    connector = session.connector
    mocker.spy(session.connector, 'close')

    await session.close()
    assert connector.close.called


def test_connector_loop(loop) -> None:
    with contextlib.ExitStack() as stack:
        another_loop = asyncio.new_event_loop()
        stack.enter_context(contextlib.closing(another_loop))

        async def make_connector():
            return TCPConnector()
        connector = another_loop.run_until_complete(make_connector())

        stack.enter_context(contextlib.closing(connector))
        with pytest.raises(RuntimeError) as ctx:
            async def make_sess():
                return ClientSession(connector=connector, loop=loop)
            loop.run_until_complete(make_sess())
        assert re.match("Session and connector have to use same event loop",
                        str(ctx.value))


def test_detach(session) -> None:
    conn = session.connector
    try:
        assert not conn.closed
        session.detach()
        assert session.connector is None
        assert session.closed
        assert not conn.closed
    finally:
        conn.close()


async def test_request_closed_session(session) -> None:
    await session.close()
    with pytest.raises(RuntimeError):
        await session.request('get', '/')


async def test_close_flag_for_closed_connector(session) -> None:
    conn = session.connector
    assert not session.closed
    await conn.close()
    assert session.closed


async def test_double_close(connector, create_session) -> None:
    session = await create_session(connector=connector)

    await session.close()
    assert session.connector is None
    await session.close()
    assert session.closed
    assert connector.closed


async def test_del(connector, loop) -> None:
    loop.set_debug(False)
    # N.B. don't use session fixture, it stores extra reference internally
    session = ClientSession(connector=connector, loop=loop)
    logs = []
    loop.set_exception_handler(lambda loop, ctx: logs.append(ctx))

    with pytest.warns(ResourceWarning):
        del session
        gc.collect()

    assert len(logs) == 1
    expected = {'client_session': mock.ANY,
                'message': 'Unclosed client session'}
    assert logs[0] == expected


async def test_del_debug(connector, loop) -> None:
    loop.set_debug(True)
    # N.B. don't use session fixture, it stores extra reference internally
    session = ClientSession(connector=connector, loop=loop)
    logs = []
    loop.set_exception_handler(lambda loop, ctx: logs.append(ctx))

    with pytest.warns(ResourceWarning):
        del session
        gc.collect()

    assert len(logs) == 1
    expected = {'client_session': mock.ANY,
                'message': 'Unclosed client session',
                'source_traceback': mock.ANY}
    assert logs[0] == expected


async def test_context_manager(connector, loop) -> None:
    with pytest.raises(TypeError):
        with ClientSession(loop=loop, connector=connector) as session:
            pass

        assert session.closed


async def test_borrow_connector_loop(connector, create_session, loop) -> None:
    session = ClientSession(connector=connector, loop=None)
    try:
        assert session._loop, loop
    finally:
        await session.close()


async def test_reraise_os_error(create_session, create_mocked_conn) -> None:
    err = OSError(1, "permission error")
    req = mock.Mock()
    req_factory = mock.Mock(return_value=req)
    req.send = mock.Mock(side_effect=err)
    session = await create_session(request_class=req_factory)

    async def create_connection(req, traces, timeout):
        # return self.transport, self.protocol
        return create_mocked_conn()
    session._connector._create_connection = create_connection
    session._connector._release = mock.Mock()

    with pytest.raises(aiohttp.ClientOSError) as ctx:
        await session.request('get', 'http://example.com')
    e = ctx.value
    assert e.errno == err.errno
    assert e.strerror == err.strerror


async def test_close_conn_on_error(create_session, create_mocked_conn) -> None:
    class UnexpectedException(BaseException):
        pass

    err = UnexpectedException("permission error")
    req = mock.Mock()
    req_factory = mock.Mock(return_value=req)
    req.send = mock.Mock(side_effect=err)
    session = await create_session(request_class=req_factory)

    connections = []
    original_connect = session._connector.connect

    async def connect(req, traces, timeout):
        conn = await original_connect(req, traces, timeout)
        connections.append(conn)
        return conn

    async def create_connection(req, traces, timeout):
        # return self.transport, self.protocol
        conn = create_mocked_conn()
        return conn

    session._connector.connect = connect
    session._connector._create_connection = create_connection
    session._connector._release = mock.Mock()

    with pytest.raises(UnexpectedException):
        async with session.request('get', 'http://example.com') as resp:
            await resp.text()

    # normally called during garbage collection.  triggers an exception
    # if the connection wasn't already closed
    for c in connections:
        c.__del__()


async def test_cookie_jar_usage(loop, aiohttp_client) -> None:
    req_url = None

    jar = mock.Mock()
    jar.filter_cookies.return_value = None

    async def handler(request):
        nonlocal req_url
        req_url = "http://%s/" % request.host

        resp = web.Response()
        resp.set_cookie("response", "resp_value")
        return resp

    app = web.Application()
    app.router.add_route('GET', '/', handler)
    session = await aiohttp_client(
        app,
        cookies={"request": "req_value"},
        cookie_jar=jar
    )

    # Updating the cookie jar with initial user defined cookies
    jar.update_cookies.assert_called_with({"request": "req_value"})

    jar.update_cookies.reset_mock()
    resp = await session.get("/")
    await resp.release()

    # Filtering the cookie jar before sending the request,
    # getting the request URL as only parameter
    jar.filter_cookies.assert_called_with(URL(req_url))

    # Updating the cookie jar with the response cookies
    assert jar.update_cookies.called
    resp_cookies = jar.update_cookies.call_args[0][0]
    assert isinstance(resp_cookies, SimpleCookie)
    assert "response" in resp_cookies
    assert resp_cookies["response"].value == "resp_value"


async def test_session_default_version(loop) -> None:
    session = aiohttp.ClientSession(loop=loop)
    assert session.version == aiohttp.HttpVersion11


async def test_session_loop(loop) -> None:
    session = aiohttp.ClientSession(loop=loop)
    with pytest.warns(DeprecationWarning):
        assert session.loop is loop
    await session.close()


def test_proxy_str(session, params) -> None:
    with mock.patch("aiohttp.client.ClientSession._request") as patched:
        session.get("http://test.example.com",
                    proxy='http://proxy.com',
                    **params)
    assert patched.called, "`ClientSession._request` not called"
    assert list(patched.call_args) == [("GET", "http://test.example.com",),
                                       dict(
                                           allow_redirects=True,
                                           proxy='http://proxy.com',
                                           **params)]


async def test_request_tracing(loop, aiohttp_client) -> None:
    async def handler(request):
        return web.json_response({'ok': True})

    app = web.Application()
    app.router.add_post('/', handler)

    trace_config_ctx = mock.Mock()
    trace_request_ctx = {}
    body = 'This is request body'
    gathered_req_body = BytesIO()
    gathered_res_body = BytesIO()
    on_request_start = mock.Mock(side_effect=asyncio.coroutine(mock.Mock()))
    on_request_redirect = mock.Mock(side_effect=asyncio.coroutine(mock.Mock()))
    on_request_end = mock.Mock(side_effect=asyncio.coroutine(mock.Mock()))

    async def on_request_chunk_sent(session, context, params):
        gathered_req_body.write(params.chunk)

    async def on_response_chunk_received(session, context, params):
        gathered_res_body.write(params.chunk)

    trace_config = aiohttp.TraceConfig(
        trace_config_ctx_factory=mock.Mock(return_value=trace_config_ctx)
    )
    trace_config.on_request_start.append(on_request_start)
    trace_config.on_request_end.append(on_request_end)
    trace_config.on_request_chunk_sent.append(on_request_chunk_sent)
    trace_config.on_response_chunk_received.append(on_response_chunk_received)
    trace_config.on_request_redirect.append(on_request_redirect)

    session = await aiohttp_client(app, trace_configs=[trace_config])

    async with session.post(
            '/', data=body, trace_request_ctx=trace_request_ctx) as resp:

        await resp.json()

        on_request_start.assert_called_once_with(
            session.session,
            trace_config_ctx,
            aiohttp.TraceRequestStartParams(
                hdrs.METH_POST,
                session.make_url('/'),
                CIMultiDict()
            )
        )

        on_request_end.assert_called_once_with(
            session.session,
            trace_config_ctx,
            aiohttp.TraceRequestEndParams(
                hdrs.METH_POST,
                session.make_url('/'),
                CIMultiDict(),
                resp
            )
        )
        assert not on_request_redirect.called
        assert gathered_req_body.getvalue() == body.encode('utf8')
        assert gathered_res_body.getvalue() == json.dumps(
            {'ok': True}).encode('utf8')


async def test_request_tracing_exception(loop) -> None:
    on_request_end = mock.Mock(side_effect=asyncio.coroutine(mock.Mock()))
    on_request_exception = mock.Mock(
        side_effect=asyncio.coroutine(mock.Mock())
    )

    trace_config = aiohttp.TraceConfig()
    trace_config.on_request_end.append(on_request_end)
    trace_config.on_request_exception.append(on_request_exception)

    with mock.patch("aiohttp.client.TCPConnector.connect") as connect_patched:
        error = Exception()
        f = loop.create_future()
        f.set_exception(error)
        connect_patched.return_value = f

        session = aiohttp.ClientSession(
            loop=loop,
            trace_configs=[trace_config]
        )

        try:
            await session.get('http://example.com')
        except Exception:
            pass

        on_request_exception.assert_called_once_with(
            session,
            mock.ANY,
            aiohttp.TraceRequestExceptionParams(
                hdrs.METH_GET,
                URL("http://example.com"),
                CIMultiDict(),
                error
            )
        )
        assert not on_request_end.called


async def test_request_tracing_interpose_headers(loop, aiohttp_client) -> None:

    async def handler(request):
        return web.Response()

    app = web.Application()
    app.router.add_get('/', handler)

    class MyClientRequest(ClientRequest):
        headers = None

        def __init__(self, *args, **kwargs):
            super(MyClientRequest, self).__init__(*args, **kwargs)
            MyClientRequest.headers = self.headers

    async def new_headers(
            session,
            trace_config_ctx,
            data):
        data.headers['foo'] = 'bar'

    trace_config = aiohttp.TraceConfig()
    trace_config.on_request_start.append(new_headers)

    session = await aiohttp_client(
        app,
        request_class=MyClientRequest,
        trace_configs=[trace_config]
    )

    await session.get('/')
    assert MyClientRequest.headers['foo'] == 'bar'


@pytest.mark.skipif(not PY_36,
                    reason="Python 3.6+ required")
def test_client_session_inheritance() -> None:
    with pytest.warns(DeprecationWarning):
        class A(ClientSession):
            pass


async def test_client_session_custom_attr(loop) -> None:
    session = ClientSession(loop=loop)
    with pytest.raises(AttributeError):
        session.custom = None


async def test_client_session_timeout_args(loop) -> None:
    session1 = ClientSession(loop=loop)
    assert session1._timeout == client.DEFAULT_TIMEOUT

    with pytest.warns(DeprecationWarning):
        session2 = ClientSession(loop=loop,
                                 read_timeout=20*60,
                                 conn_timeout=30*60)
    assert session2._timeout == client.ClientTimeout(total=20*60,
                                                     connect=30*60)

    with pytest.raises(ValueError):
        ClientSession(loop=loop,
                      timeout=client.ClientTimeout(total=10*60),
                      read_timeout=20*60)

    with pytest.raises(ValueError):
        ClientSession(loop=loop,
                      timeout=client.ClientTimeout(total=10 * 60),
                      conn_timeout=30 * 60)


async def test_requote_redirect_url_default() -> None:
    session = ClientSession()
    assert session.requote_redirect_url


async def test_requote_redirect_url_default_disable() -> None:
    session = ClientSession(requote_redirect_url=False)
    assert not session.requote_redirect_url


async def test_requote_redirect_setter() -> None:
    session = ClientSession()
    assert session.requote_redirect_url
    with pytest.warns(DeprecationWarning):
        session.requote_redirect_url = False
    assert not session.requote_redirect_url
