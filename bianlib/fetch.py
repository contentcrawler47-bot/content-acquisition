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

There are TWO breakers, and they answer different questions.

The REQUEST breaker (`CONSECUTIVE_FAILURE_LIMIT`) counts requests that
exhausted every attempt. It is the right instrument for a source that is
answering but refusing -- a run of 503s -- because each refusal is one request
and the limit is reached in a bounded number of them.

The TRANSPORT breaker (`TRANSPORT_FAILURE_LIMIT`) counts consecutive
connection-level failures: timeouts, refused connections, resets, DNS. It
exists because the request breaker cannot trip on an unreachable host inside a
30-minute job. One request against a dead host costs up to three transport
tries per attempt (keep-alive, reconnect, urllib), each bounded by the 90 s
timeout, across four attempts: 3 x 90 s x 4 = 18 minutes for ONE request, so
eight of them is ~144 minutes. The transport breaker counts the tries instead
of the requests, so it trips after 8 x 90 s = 12 minutes at worst, plus the
backoff sleeps between attempts, and in seconds when the failure is fast.

Every transport try is recorded on the Response -- including the ones that
were retried past. The first two tries per request used to be swallowed by a
bare except and reconnected in silence, which is why a runner-region failure
on 30 August 2026 could be diagnosed no further than "it timed out". Nothing
here is discarded any more: a failure that is retried past is written down,
and a request that gives up carries its attempt log on the exception.

The Response carries the DECODED ENTITY BODY as bytes, alongside the text. The
bytes are what a raw store keeps and digests; the declaration that the digest
is over the decoded entity rather than the transfer encoding is recorded in
the project's design documents, and `Content-Encoding` travels with the
response headers so the decision is reversible in principle.

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

#: Consecutive transport-level failures (no HTTP response at all) before the
#: transport breaker trips. Counted per TRY, not per request, and reset by any
#: response the server sends -- a 503 is a response, so a refusing-but-
#: reachable source is the request breaker's problem, not this one's.
#:
#: Worst case to trip: 8 tries x DEFAULT_TIMEOUT = 12 minutes, plus the
#: backoff sleeps between attempts (at most 2 + 4 + 8 s within one request).
#: That fits inside a 30-minute job with room to upload what was fetched.
TRANSPORT_FAILURE_LIMIT = 8


class SourceUnhappy(RuntimeError):
    """Raised when a circuit breaker trips.

    Distinct from a single failed fetch: this means the source has failed
    repeatedly in a row and the right response is to stop, not to continue
    with the remaining pages. `attempts` carries the try log of the request
    that tripped it.
    """

    def __init__(self, message: str, attempts: list | None = None):
        super().__init__(message)
        self.attempts = list(attempts or [])


class Response:
    """One answer from the source.

    `text` is what the parsers read. `body` is the same entity as bytes,
    after transfer decoding (gzip) and before character decoding -- the form
    a raw store keeps. `headers` are the response headers as received and
    `request_headers` the ones actually sent, so a capture record can say
    what was asked and what came back without a second copy of either.
    `attempts` is every transport try this request made, in order, including
    the ones that failed and were retried past.
    """

    __slots__ = ("status", "text", "etag", "last_modified", "from_cache",
                 "body", "headers", "request_headers", "attempts")

    def __init__(self, status, text, etag="", last_modified="",
                 from_cache=False, body=b"", headers=None,
                 request_headers=None, attempts=None):
        self.status = status
        self.text = text
        self.etag = etag
        self.last_modified = last_modified
        self.from_cache = from_cache
        self.body = body
        self.headers = dict(headers or {})
        self.request_headers = dict(request_headers or {})
        self.attempts = list(attempts or [])


def _inflate(raw: bytes, encoding: str) -> bytes:
    """The entity body: transfer-decoded, still bytes."""
    if (encoding or "").lower() == "gzip":
        try:
            return gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        except OSError:
            # Some servers advertise gzip and send plain bytes. Treat the
            # header as advisory rather than failing the fetch.
            return raw
    return raw


def _decode(raw: bytes, encoding: str) -> str:
    return _inflate(raw, encoding).decode("utf-8", errors="replace")


def _header(headers: dict, name: str) -> str:
    """A response header by name, whichever case the server used."""
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value or ""
    return ""


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
                 failure_limit: int = CONSECUTIVE_FAILURE_LIMIT,
                 transport_failure_limit: int = TRANSPORT_FAILURE_LIMIT):
        self.base = base.rstrip("/")
        parts = urllib.parse.urlsplit(self.base)
        self.host = parts.netloc
        self.scheme = parts.scheme or "https"
        self.delay = max(0.0, float(delay))
        self.timeout = timeout
        self.max_attempts = max(1, int(max_attempts))
        self.failure_limit = max(1, int(failure_limit))
        self.transport_failure_limit = max(1, int(transport_failure_limit))

        #: {url: {"etag": ..., "last_modified": ...}} carried between runs so
        #: an unchanged page costs the source a 304 and no body.
        self.validators = dict(validators or {})

        self._conn = None
        self._last_request = 0.0
        self._consecutive_failures = 0
        self._consecutive_transport_failures = 0

        # Reported at the end of every chunk. Counts and bytes only — never
        # content: Actions logs on a public repo are world-readable.
        #
        # `transport_errors` counts every connection-level failure, including
        # the ones a later try recovered from. Before it existed, a request
        # that reconnected twice and then succeeded looked identical to one
        # that succeeded first time.
        self.stats = {"requests": 0, "not_modified": 0, "retries": 0,
                      "bytes": 0, "waited": 0.0, "failures": 0,
                      "reconnects": 0, "transport_errors": 0}

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

    @staticmethod
    def request_headers_template() -> dict:
        """The headers every request starts from. Exposed so a capture
        record can state the policy in force without a second copy of it."""
        return {"User-Agent": UA,
                "Accept-Encoding": "gzip",
                "Connection": "keep-alive"}

    def _transport_failed(self, record: dict, url: str, attempts: list):
        """Record one connection-level failure and count it towards the
        transport breaker. Raises if the breaker trips."""
        self.stats["transport_errors"] += 1
        self._consecutive_transport_failures += 1
        print(f"    {record['error']} on {url.rsplit('/', 1)[-1]} "
              f"via {record['transport']}", flush=True)
        if self._consecutive_transport_failures >= self.transport_failure_limit:
            raise SourceUnhappy(
                f"{self._consecutive_transport_failures} consecutive transport "
                f"failures (last: {record['error']}). The host is not "
                f"answering at all; stopping rather than waiting out the "
                f"timeout on every remaining page.", attempts)

    def _request_once(self, url: str, headers: dict, attempt: int,
                      attempts: list):
        """One attempt: the persistent connection, then a reconnect, then
        urllib. Every try is appended to `attempts` whether it succeeded or
        not, so nothing that happened here is invisible afterwards.

        Returns (status, body_bytes, response_headers).
        """
        parts = urllib.parse.urlsplit(url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")

        def record(transport: str, status=None, error: str = "") -> dict:
            entry = {"attempt": attempt, "transport": transport,
                     "status": status, "error": error,
                     "at": time.time()}
            attempts.append(entry)
            return entry

        if parts.netloc == self.host:
            for n in (1, 2):
                transport = "keep-alive" if n == 1 else "reconnect"
                if self._conn is None:
                    self._conn = self._connect()
                    if n == 2:
                        self.stats["reconnects"] += 1
                try:
                    self._conn.request("GET", path, headers=headers)
                    resp = self._conn.getresponse()
                    body = resp.read()      # must drain to reuse the socket
                    record(transport, status=resp.status)
                    self._consecutive_transport_failures = 0
                    return resp.status, body, dict(resp.getheaders())
                except (http.client.HTTPException, OSError) as e:
                    self.close()
                    entry = record(transport, error=f"{type(e).__name__}: {e}")
                    self._transport_failed(entry, url, attempts)
            # Persistent connection is not working; fall through to urllib.

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read()
                record("urllib", status=r.status)
                self._consecutive_transport_failures = 0
                return r.status, body, dict(r.headers)
        except urllib.error.HTTPError as e:
            # An HTTP error is a response: the host answered.
            record("urllib", status=e.code)
            self._consecutive_transport_failures = 0
            return e.code, e.read() or b"", dict(e.headers or {})
        except Exception as e:                          # transport, not HTTP
            entry = record("urllib", error=f"{type(e).__name__}: {e}")
            self._transport_failed(entry, url, attempts)
            raise

    def get(self, url: str, conditional: bool = True) -> Response:
        """Fetch one URL, politely. Raises on give-up; never returns None.

        A 304 comes back with `from_cache` set and an empty body, so callers
        that keep their own copy can skip the work entirely. A raised
        exception carries `attempts`, the full try log, so a request that
        gave up is as diagnosable as one that succeeded.
        """
        headers = self.request_headers_template()
        known = self.validators.get(url) if conditional else None
        if known:
            if known.get("etag"):
                headers["If-None-Match"] = known["etag"]
            if known.get("last_modified"):
                headers["If-Modified-Since"] = known["last_modified"]

        attempts: list = []
        last_error = ""
        for attempt in range(1, self.max_attempts + 1):
            self._pace()
            try:
                status, body, resp_headers = self._request_once(
                    url, headers, attempt, attempts)
            except SourceUnhappy:
                self.stats["failures"] += 1
                raise
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
                                from_cache=True, body=b"", headers=resp_headers,
                                request_headers=headers, attempts=attempts)

            if 200 <= status < 300:
                self._consecutive_failures = 0
                etag = _header(resp_headers, "ETag")
                lm = _header(resp_headers, "Last-Modified")
                if etag or lm:
                    self.validators[url] = {"etag": etag, "last_modified": lm}
                entity = _inflate(body, _header(resp_headers, "Content-Encoding"))
                text = entity.decode("utf-8", errors="replace")
                return Response(status, text, etag, lm, body=entity,
                                headers=resp_headers, request_headers=headers,
                                attempts=attempts)

            # 404 is a real answer, not a fault — shards and views legitimately
            # go missing between versions and the caller decides what that
            # means. Retrying it would be pure noise. The body is kept: a 404
            # page is still an artifact a raw store can record.
            if status == 404:
                self._consecutive_failures = 0
                entity = _inflate(body, _header(resp_headers, "Content-Encoding"))
                return Response(404, "", body=entity, headers=resp_headers,
                                request_headers=headers, attempts=attempts)

            last_error = last_error or f"HTTP {status}"
            if attempt < self.max_attempts:
                wait = self._retry_after(
                    _header(resp_headers, "Retry-After"), attempt)
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
                f"request from a source that is refusing.", attempts)
        err = urllib.error.URLError(last_error or "request failed")
        err.attempts = attempts
        raise err

    # -- reporting -------------------------------------------------------

    def report(self) -> str:
        s = self.stats
        return (f"{s['requests']} requests, {s['not_modified']} not-modified, "
                f"{s['bytes'] / 1024 / 1024:.1f} MB, {s['retries']} retries, "
                f"{s['failures']} failures, {s['transport_errors']} transport "
                f"errors, {s['waited']:.0f}s paced")


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
