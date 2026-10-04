"""Polite HTTP client for scraping (build guide Phase 1 step 6).

- at most one request per `min_interval_s` per host, custom User-Agent
- robots.txt is checked before every fetch
- every response is cached on disk, so re-runs make no requests
- transient failures (timeouts, 5xx) are retried with exponential backoff
- binary downloads stream to a .part file and resume with HTTP Range requests, so a
  dropped connection does not restart a large PDF from zero; resuming continues while
  attempts make progress and stops after 3 attempts in a row without new bytes
- a block (403/429/530 or a firewall page) raises BlockedError immediately and is never
  retried: "If a site blocks you, download manually. Don't fight it."
"""

import hashlib
import logging
import time
import urllib.robotparser
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)

_BLOCK_STATUSES = frozenset({401, 403, 429, 530})
_BLOCK_MARKERS = (b"Unauthorized Request Blocked", b"Access Denied")


class BlockedError(RuntimeError):
    """The site refused us. Stop and fall back to manual download."""


class DisallowedError(RuntimeError):
    """robots.txt disallows this URL for our User-Agent."""


class TransientHTTPError(RuntimeError):
    """A retryable failure (timeout, connection error, 5xx)."""


class PoliteClient:
    MAX_STALLED_ATTEMPTS = 3
    # A server can trickle bytes just fast enough to dodge the read timeout; cap each
    # download attempt and resume with a fresh request instead.
    MAX_ATTEMPT_S = 180.0

    def __init__(
        self,
        cache_dir: Path,
        user_agent: str,
        min_interval_s: float = 2.5,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cache_dir = cache_dir
        self.user_agent = user_agent
        self.min_interval_s = min_interval_s
        self._sleep = sleep
        self._clock = clock
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._client = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=httpx.Timeout(60.0, connect=20.0),
            follow_redirects=True,
            transport=transport,
        )
        self.requests_made = 0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PoliteClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- public API ---------------------------------------------------------------------

    def get_text(self, url: str) -> str:
        """GET a page, using the disk cache when available."""
        cached = self._cache_path(url)
        if cached.exists():
            return cached.read_text(encoding="utf-8")
        body = self._fetch(url)
        cached.parent.mkdir(parents=True, exist_ok=True)
        text = body.decode("utf-8", errors="replace")
        cached.write_text(text, encoding="utf-8")
        return text

    def download(self, url: str, dest: Path) -> None:
        """Download a binary file to `dest`, resuming a previous partial download."""
        if not self._allowed(url):
            raise DisallowedError(f"robots.txt disallows {url}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(dest.suffix + ".part")
        stalled = 0
        while True:
            before = part.stat().st_size if part.exists() else 0
            try:
                self._download_once(url, part)
                break
            except TransientHTTPError:
                after = part.stat().st_size if part.exists() else 0
                # Keep resuming while attempts make progress; give up after
                # MAX_STALLED_ATTEMPTS in a row that add no bytes.
                stalled = 0 if after > before else stalled + 1
                if stalled >= self.MAX_STALLED_ATTEMPTS:
                    raise
                self._sleep(min(5.0 * 2**stalled, 60.0))
        part.replace(dest)

    # -- internals ----------------------------------------------------------------------

    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode()).hexdigest()
        return self.cache_dir / "http" / f"{digest}.html"

    def _throttle(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is not None:
            wait = self.min_interval_s - (self._clock() - last)
            if wait > 0:
                self._sleep(wait)
        self._last_request[host] = self._clock()

    def _allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        parser = self._robots.get(origin)
        if parser is None:
            parser = urllib.robotparser.RobotFileParser()
            self._throttle(parts.netloc)
            self.requests_made += 1
            resp = self._client.get(f"{origin}/robots.txt")
            if resp.status_code in _BLOCK_STATUSES:
                raise BlockedError(f"{origin}/robots.txt returned HTTP {resp.status_code}")
            # No robots.txt (404 etc.) means no restrictions.
            parser.parse(resp.text.splitlines() if resp.status_code == 200 else [])
            self._robots[origin] = parser
        return parser.can_fetch(self.user_agent, url)

    def _fetch(self, url: str) -> bytes:
        if not self._allowed(url):
            raise DisallowedError(f"robots.txt disallows {url}")
        return self._fetch_with_retry(url)

    @retry(
        retry=retry_if_exception_type(TransientHTTPError),
        wait=wait_exponential(multiplier=5, max=60),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def _fetch_with_retry(self, url: str) -> bytes:
        self._throttle(urlsplit(url).netloc)
        self.requests_made += 1
        try:
            resp = self._client.get(url)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            log.warning("transient error for %s: %s", url, exc)
            raise TransientHTTPError(str(exc)) from exc
        if resp.status_code in _BLOCK_STATUSES or any(
            marker in resp.content[:4096] for marker in _BLOCK_MARKERS
        ):
            raise BlockedError(f"{url} returned HTTP {resp.status_code} (blocked)")
        if resp.status_code >= 500:
            raise TransientHTTPError(f"{url} returned HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise httpx.HTTPStatusError(
                f"{url} returned HTTP {resp.status_code}", request=resp.request, response=resp
            )
        return resp.content

    def _download_once(self, url: str, part: Path) -> None:
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        self._throttle(urlsplit(url).netloc)
        self.requests_made += 1
        try:
            with self._client.stream("GET", url, headers=headers) as resp:
                if resp.status_code in _BLOCK_STATUSES:
                    raise BlockedError(f"{url} returned HTTP {resp.status_code} (blocked)")
                if resp.status_code == 416:  # our partial file is unusable; start over
                    part.unlink(missing_ok=True)
                    raise TransientHTTPError(f"{url}: range not satisfiable, restarting")
                if resp.status_code >= 500:
                    raise TransientHTTPError(f"{url} returned HTTP {resp.status_code}")
                if resp.status_code == 206:
                    mode, start = "ab", have
                    total = int(resp.headers["content-range"].rsplit("/", 1)[1])
                elif resp.status_code == 200:  # server ignored the range: start over
                    mode, start = "wb", 0
                    length = resp.headers.get("content-length")
                    total = int(length) if length is not None else -1
                else:
                    raise httpx.HTTPStatusError(
                        f"{url} returned HTTP {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                started = self._clock()
                with part.open(mode) as fh:
                    first = True
                    for chunk in resp.iter_bytes():
                        if self._clock() - started > self.MAX_ATTEMPT_S:
                            raise TransientHTTPError(f"{url}: attempt exceeded time cap")
                        if first and start == 0 and any(m in chunk[:4096] for m in _BLOCK_MARKERS):
                            raise BlockedError(f"{url} returned a block page")
                        first = False
                        fh.write(chunk)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            got = part.stat().st_size if part.exists() else 0
            log.warning("transient error for %s after %d bytes: %s", url, got, exc)
            raise TransientHTTPError(str(exc)) from exc
        size = part.stat().st_size
        if total >= 0 and size != total:
            raise TransientHTTPError(f"{url}: got {size} of {total} bytes")
