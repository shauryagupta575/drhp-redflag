from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from app.ingest.http import BlockedError, DisallowedError, PoliteClient, TransientHTTPError

ROBOTS = "User-agent: *\nDisallow: /private\n"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # tenacity's backoff would otherwise really sleep between retries.
    for method in (PoliteClient._fetch_with_retry, PoliteClient._download_with_retry):
        monkeypatch.setattr(method.retry, "sleep", lambda s: None)  # type: ignore[union-attr]
    yield


def make_client(
    tmp_path: Path,
    handler: Callable[[httpx.Request], httpx.Response],
    clock: FakeClock | None = None,
    interval: float = 2.5,
) -> PoliteClient:
    clock = clock or FakeClock()
    return PoliteClient(
        tmp_path,
        "test-agent/1.0",
        min_interval_s=interval,
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        clock=clock.time,
    )


def site(
    pages: dict[str, httpx.Response], log: list[str]
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        return pages.get(request.url.path, httpx.Response(404))

    return handler


def test_sends_user_agent_and_caches_pages(tmp_path: Path) -> None:
    seen: list[str] = []
    agents: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        agents.append(request.headers["user-agent"])
        return site({"/a": httpx.Response(200, text="hello")}, seen)(request)

    with make_client(tmp_path, handler) as client:
        assert client.get_text("https://example.test/a") == "hello"
        assert client.get_text("https://example.test/a") == "hello"
    assert seen == ["/robots.txt", "/a"]  # second read came from the disk cache
    assert set(agents) == {"test-agent/1.0"}
    with make_client(tmp_path, handler) as fresh:  # cache survives a new client
        assert fresh.get_text("https://example.test/a") == "hello"
    assert seen == ["/robots.txt", "/a"]


def test_throttles_requests_per_host(tmp_path: Path) -> None:
    clock = FakeClock()
    log: list[str] = []
    pages = {"/a": httpx.Response(200, text="a"), "/b": httpx.Response(200, text="b")}
    with make_client(tmp_path, site(pages, log), clock) as client:
        client.get_text("https://example.test/a")
        client.get_text("https://example.test/b")
    # robots.txt, /a and /b: each request after the first waits the full interval.
    assert clock.slept == [2.5, 2.5]


def test_no_wait_when_interval_already_elapsed(tmp_path: Path) -> None:
    clock = FakeClock()
    log: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        clock.now += 10  # a slow response
        return site({"/a": httpx.Response(200), "/b": httpx.Response(200)}, log)(request)

    with make_client(tmp_path, handler, clock) as client:
        client.get_text("https://example.test/a")
        client.get_text("https://example.test/b")
    assert clock.slept == []


def test_robots_disallow_is_respected(tmp_path: Path) -> None:
    log: list[str] = []
    with make_client(tmp_path, site({}, log)) as client:
        with pytest.raises(DisallowedError):
            client.get_text("https://example.test/private/x")
    assert log == ["/robots.txt"]


def test_missing_robots_means_allowed(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text="ok")

    with make_client(tmp_path, handler) as client:
        assert client.get_text("https://example.test/private/x") == "ok"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(530, text="Unauthorized Request Blocked"),
        httpx.Response(403),
        httpx.Response(429),
        httpx.Response(200, text="<html>Unauthorized Request Blocked</html>"),
    ],
)
def test_block_stops_immediately_without_retry(tmp_path: Path, response: httpx.Response) -> None:
    log: list[str] = []
    with make_client(tmp_path, site({"/a": response}, log)) as client:
        with pytest.raises(BlockedError):
            client.get_text("https://example.test/a")
    assert log.count("/a") == 1
    assert not (tmp_path / "http").exists()  # nothing cached


def test_transient_errors_are_retried(tmp_path: Path) -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectTimeout("slow")
        if attempts["n"] == 2:
            return httpx.Response(503)
        return httpx.Response(200, text="finally")

    with make_client(tmp_path, handler) as client:
        assert client.get_text("https://example.test/a") == "finally"
    assert attempts["n"] == 3


def test_transient_errors_give_up_after_four_attempts(tmp_path: Path) -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        attempts["n"] += 1
        return httpx.Response(502)

    with make_client(tmp_path, handler) as client:
        with pytest.raises(TransientHTTPError):
            client.get_text("https://example.test/a")
    assert attempts["n"] == 4


def test_not_found_raises_without_retry(tmp_path: Path) -> None:
    log: list[str] = []
    with make_client(tmp_path, site({}, log)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            client.get_text("https://example.test/missing")
    assert log.count("/missing") == 1


def test_download_writes_file_atomically(tmp_path: Path) -> None:
    log: list[str] = []
    with make_client(
        tmp_path, site({"/f.pdf": httpx.Response(200, content=b"%PDF-1.7")}, log)
    ) as client:
        dest = tmp_path / "out" / "f.pdf"
        client.download("https://example.test/f.pdf", dest)
    assert dest.read_bytes() == b"%PDF-1.7"
    assert not list((tmp_path / "out").glob("*.part"))


def dropping_stream(first: bytes) -> Iterator[bytes]:
    yield first
    raise httpx.RemoteProtocolError("peer closed connection without sending complete body")


def test_download_resumes_after_dropped_connection(tmp_path: Path) -> None:
    full = b"%PDF-1234567890"
    ranges: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        ranges.append(request.headers.get("range"))
        if len(ranges) == 1:
            return httpx.Response(
                200, headers={"Content-Length": str(len(full))}, content=dropping_stream(full[:6])
            )
        start = int(request.headers["range"].removeprefix("bytes=").rstrip("-"))
        return httpx.Response(
            206,
            headers={"Content-Range": f"bytes {start}-{len(full) - 1}/{len(full)}"},
            content=full[start:],
        )

    dest = tmp_path / "f.pdf"
    with make_client(tmp_path, handler) as client:
        client.download("https://example.test/f.pdf", dest)
    assert dest.read_bytes() == full
    assert ranges == [None, "bytes=6-"]
    assert not dest.with_suffix(".pdf.part").exists()


def test_download_resumes_partial_file_from_earlier_run(tmp_path: Path) -> None:
    full = b"%PDF-abcdef"
    (tmp_path / "f.pdf.part").write_bytes(full[:4])
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        seen.append(request.headers.get("range"))
        return httpx.Response(
            206, headers={"Content-Range": f"bytes 4-10/{len(full)}"}, content=full[4:]
        )

    with make_client(tmp_path, handler) as client:
        client.download("https://example.test/f.pdf", tmp_path / "f.pdf")
    assert (tmp_path / "f.pdf").read_bytes() == full
    assert seen == ["bytes=4-"]


def test_download_restarts_when_server_ignores_range(tmp_path: Path) -> None:
    full = b"%PDF-whole-file"
    (tmp_path / "f.pdf.part").write_bytes(b"garbage")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, headers={"Content-Length": str(len(full))}, content=full)

    with make_client(tmp_path, handler) as client:
        client.download("https://example.test/f.pdf", tmp_path / "f.pdf")
    assert (tmp_path / "f.pdf").read_bytes() == full


def test_download_short_transfer_is_retried(tmp_path: Path) -> None:
    full = b"%PDF-0123456789"
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        calls["n"] += 1
        if calls["n"] == 1:  # claims the full length but sends less, then closes cleanly
            return httpx.Response(
                200,
                headers={"Content-Length": str(len(full))},
                content=iter([full[:5]]),
            )
        start = int(request.headers["range"].removeprefix("bytes=").rstrip("-"))
        return httpx.Response(
            206, headers={"Content-Range": f"bytes {start}-14/{len(full)}"}, content=full[start:]
        )

    with make_client(tmp_path, handler) as client:
        client.download("https://example.test/f.pdf", tmp_path / "f.pdf")
    assert (tmp_path / "f.pdf").read_bytes() == full
    assert calls["n"] == 2


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(530, text="Unauthorized Request Blocked"),
        httpx.Response(200, text="<html>Access Denied</html>"),
    ],
)
def test_download_block_stops_without_retry(tmp_path: Path, response: httpx.Response) -> None:
    log: list[str] = []
    with make_client(tmp_path, site({"/f.pdf": response}, log)) as client:
        with pytest.raises(BlockedError):
            client.download("https://example.test/f.pdf", tmp_path / "f.pdf")
    assert log.count("/f.pdf") == 1
    assert not (tmp_path / "f.pdf").exists()


def test_download_respects_robots(tmp_path: Path) -> None:
    log: list[str] = []
    with make_client(tmp_path, site({}, log)) as client:
        with pytest.raises(DisallowedError):
            client.download("https://example.test/private/f.pdf", tmp_path / "f.pdf")
    assert log == ["/robots.txt"]
