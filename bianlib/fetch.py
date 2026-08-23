"""
Polite HTTP for a source that will be asked for thousands of pages.

The semantic harvest is 47 requests. The full landscape adds ~1,231 view pages
on top, and that is a different kind of demand on someone else's web server.
Everything here exists to keep that demand modest and well-behaved:

    pacing            a floor on the interval between requests, so a burst of
                      1,231 fetches spreads out instead of arriving at once
    one at a time     no concurrency anywhere; the workflow also pins itself to
                      a single runner, so there is never a second client
    keep-alive        one TLS handshake per chunk instead of one per page
    gzip              the shards are ~55 MB uncompressed and ~8 MB compressed
    conditional GET   an ETag from the previous run turns an unchanged page
                      into a 304 with no body at all
    backoff           429 and 5xx are honoured, Retry-After included, rather
                      than retried immediately
    circuit breaker   a run of consecutive failures aborts the chunk instead of
                      hammering a source that is evidently unhappy

The breaker matters more than it looks. Without it a source that starts
returning 503 receives 1,231 requests and five retries each before anyone
notices, which is precisely the behaviour that gets a crawler blocked.

Standard library only, in keeping with the rest of the repo.
"""

from __future__ import annotations

import gzip
import http.client
import io
import time
import urllib.error
import urllib.parse
import urllib.request

#: Identifies the harvester and where to complain about it. The
#: "Mozilla/5.0 (compatible; ...)" shape is kept because naive user-agent
#: filters reject anything that does not have it, but the real name and a
#: contact URL follow so this is not pretending to be a browser.
UA = ("Mozilla/5.0 (compatible; content-acquisition/1.0; "
      "+https://github.com/contentcrawler47-bot/content-acquisition)")

DEFAULT_DELAY = 1.0          # seconds between the start of consecutive requests
DEFAULT_TIMEOUT = 90
MAX_ATTEMPTS = 4
MAX_BACKOFF = 60.0
CONSECUTIVE_FAILURE_LIMIT = 8


class SourceUnhappy(RuntimeError):
    """Raised when the circuit breaker trips.

    Distinct from a single failed fetch: this means the source has failed
    repeatedly in a row and the right response is to stop, not to continue
    with the remaining pages.
    """


class Response:
    __slots__ = ("status", "text", "etag", "last_modified", "from_cache")

    def __init__(self, status, text, etag="", last_modified="",
                 from_cache=False):
        self.status = status
        self.text = text
        self.etag = etag
        self.last_modified = last_modified
        self.from_cache = from_cache


def _decode(raw: bytes, encoding: str) -> str:
    if (encoding or "").lower() == "gzip":
        try:
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        except OSError:
            # Some servers advertise gzip and send plain bytes. Treat the
            # header as advisory rather than failing the fetch.
            pass
    return raw.decode("utf-8", errors="replace")


class Fetcher:
    """A single-threaded, self-limiting HTTP client for one host.

    Reuses one connection where it can and falls back to urllib whenever the
    persistent connection misbehaves, because a hand-rolled keep-alive client
    that cannot recover is worse than no keep-alive at all.
    """

    def __init__(self, base: str, delay: float = DEFAULT_DELAY,
                 timeout: int = DEFAULT_TIMEOUT,
                 max_attempts: int = MAX_ATTEMPTS,
                 validators: dict | None = None,
                 failure_limit: int = CONSECUTIVE_FAILURE_LIMIT):
        self.base = base.rstrip("/")
        parts = urllib.parse.urlsplit(self.base)
        self.host = parts.netloc
        self.scheme = parts.scheme or "https"
        self.delay = max(0.0, float(delay))
        self.timeout = timeout
        self.max_attempts = max(1, int(max_attempts))
        self.failure_limit = max(1, int(failure_limit))

        #: {url: {"etag": ..., "last_modified": ...}} carried between runs so
        #: an unchanged page costs the source a 304 and no body.
        self.validators = dict(validators or {})

        self._conn = None
        self._last_request = 0.0
        self._consecutive_failures = 0

        # Reported at the end of every chunk. Counts and bytes only — never
        # content: Actions logs on a public repo are world-readable.
        self.stats = {"requests": 0, "not_modified": 0, "retries": 0,
                      "bytes": 0, "waited": 0.0, "failures": 0,
                      "reconnects": 0}

    # -- connection ------------------------------------------------------

    def _connect(self):
        if self.scheme == "http":
            return http.client.HTTPConnection(self.host, timeout=self.timeout)
        return http.client.HTTPSConnection(self.host, timeout=self.timeout)

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    # -- pacing ----------------------------------------------------------

    def _pace(self):
        gap = time.monotonic() - self._last_request
        if gap < self.delay:
            time.sleep(self.delay - gap)
            self.stats["waited"] += self.delay - gap
        self._last_request = time.monotonic()

    @staticmethod
    def _retry_after(value: str, attempt: int) -> float:
        """Honour Retry-After when it is sane, otherwise back off doubling."""
        try:
            wait = float((value or "").strip())
        except ValueError:
            wait = 0.0
        if wait <= 0:
            wait = min(MAX_BACKOFF, 2.0 ** attempt)
        return min(MAX_BACKOFF, wait)

    # -- fetching --------------------------------------------------------

    def _request_once(self, url: str, headers: dict):
        """One attempt over the persistent connection, falling back to urllib.

        Returns (status, body_bytes, response_headers).
        """
        parts = urllib.parse.urlsplit(url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        if parts.netloc == self.host:
            for attempt in (1, 2):
                if self._conn is None:
                    self._conn = self._connect()
                    if attempt == 2:
                        self.stats["reconnects"] += 1
                try:
                    self._conn.request("GET", path, headers=headers)
                    resp = self._conn.getresponse()
                    body = resp.read()      # must drain to reuse the socket
                    return resp.status, body, dict(resp.getheaders())
                except (http.client.HTTPException, OSError):
                    self.close()
                    if attempt == 2:
                        break
            # Persistent connection is not working; fall through to urllib.

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read() or b"", dict(e.headers or {})

    def get(self, url: str, conditional: bool = True) -> Response:
        """Fetch one URL, politely. Raises on give-up; never returns None.

        A 304 comes back with `from_cache` set and an empty body, so callers
        that keep their own copy can skip the work entirely.
        """
        headers = {"User-Agent": UA,
                   "Accept-Encoding": "gzip",
                   "Connection": "keep-alive"}
        known = self.validators.get(url) if conditional else None
        if known:
            if known.get("etag"):
                headers["If-None-Match"] = known["etag"]
            if known.get("last_modified"):
                headers["If-Modified-Since"] = known["last_modified"]

        last_error = ""
        for attempt in range(1, self.max_attempts + 1):
            self._pace()
            try:
                status, body, resp_headers = self._request_once(url, headers)
            except Exception as e:                      # transport, not HTTP
                last_error = f"{type(e).__name__}: {e}"
                status, body, resp_headers = 0, b"", {}

            self.stats["requests"] += 1
            self.stats["bytes"] += len(body)

            if status == 304:
                self.stats["not_modified"] += 1
                self._consecutive_failures = 0
                return Response(304, "", known.get("etag", "") if known else "",
                                known.get("last_modified", "") if known else "",
                                from_cache=True)

            if 200 <= status < 300:
                self._consecutive_failures = 0
                etag = resp_headers.get("ETag") or resp_headers.get("etag") or ""
                lm = (resp_headers.get("Last-Modified")
                      or resp_headers.get("last-modified") or "")
                if etag or lm:
                    self.validators[url] = {"etag": etag, "last_modified": lm}
                text = _decode(body, resp_headers.get("Content-Encoding")
                               or resp_headers.get("content-encoding") or "")
                return Response(status, text, etag, lm)

            # 404 is a real answer, not a fault — shards and views legitimately
            # go missing between versions and the caller decides what that
            # means. Retrying it would be pure noise.
            if status == 404:
                self._consecutive_failures = 0
                return Response(404, "")

            last_error = last_error or f"HTTP {status}"
            if attempt < self.max_attempts:
                wait = self._retry_after(
                    resp_headers.get("Retry-After")
                    or resp_headers.get("retry-after") or "", attempt)
                self.stats["retries"] += 1
                print(f"    {status or 'error'} on {url.rsplit('/', 1)[-1]} — "
                      f"waiting {wait:.0f}s (attempt {attempt}/"
                      f"{self.max_attempts})", flush=True)
                time.sleep(wait)

        self.stats["failures"] += 1
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_limit:
            raise SourceUnhappy(
                f"{self._consecutive_failures} consecutive failures "
                f"(last: {last_error}). Stopping rather than continuing to "
                f"request from a source that is refusing.")
        raise urllib.error.URLError(last_error or "request failed")

    # -- reporting -------------------------------------------------------

    def report(self) -> str:
        s = self.stats
        return (f"{s['requests']} requests, {s['not_modified']} not-modified, "
                f"{s['bytes'] / 1024 / 1024:.1f} MB, {s['retries']} retries, "
                f"{s['failures']} failures, {s['waited']:.0f}s paced")


def robots_disallows(fetcher: Fetcher, path: str) -> str:
    """Whether robots.txt forbids `path` for us. Returns the rule, or "".

    Checked once per run before anything else is fetched. A missing or
    unreadable robots.txt means no restriction — that is what the standard
    says, and inventing a stricter reading would just stop the harvest for no
    reason.
    """
    root = f"{fetcher.scheme}://{fetcher.host}"
    try:
        resp = fetcher.get(f"{root}/robots.txt", conditional=False)
    except Exception:
        return ""
    if resp.status != 200 or not resp.text.strip():
        return ""

    applicable, rules = False, []
    for line in resp.text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field, value = field.strip().lower(), value.strip()
        if field == "user-agent":
            applicable = value == "*" or "content-acquisition" in value.lower()
        elif field == "disallow" and applicable and value:
            rules.append(value)
    for rule in rules:
        if path.startswith(rule):
            return rule
    return ""
