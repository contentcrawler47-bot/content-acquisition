"""
Connectivity probing with diagnosis.

The point of this module is that a failure says *what* went wrong and *what to
check*, rather than surfacing a bare traceback. Sources declare what they need
to reach; this reaches it and classifies whatever happens.
"""

from __future__ import annotations

import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

DEFAULT_TIMEOUT = 45
UA = "Mozilla/5.0 (compatible; content-acquisition/1.0)"


@dataclass
class ProbeSpec:
    """What a source expects to be able to reach."""
    label: str
    url: str
    #: Body must start with this once whitespace-stripped. Catches a server
    #: returning an error page or login redirect with HTTP 200.
    expect_prefix: str | None = None
    #: Body must contain this somewhere in the first 4 KB.
    expect_contains: str | None = None
    min_bytes: int = 1
    #: A probe that may legitimately be absent (optional data file).
    optional: bool = False


@dataclass
class ProbeResult:
    spec: ProbeSpec
    ok: bool
    status: int | None = None
    bytes_read: int = 0
    seconds: float = 0.0
    final_url: str = ""
    cause: str = ""            # short classification
    detail: str = ""           # what was observed
    hint: str = ""             # what to check
    body_head: str = field(default="", repr=False)   # never logged

    @property
    def summary(self) -> str:
        if self.ok:
            kb = self.bytes_read / 1024
            return f"HTTP {self.status}, {kb:,.0f} KB, {self.seconds:.1f}s"
        return f"{self.cause}: {self.detail}" if self.detail else self.cause


def probe(spec: ProbeSpec, timeout: int = DEFAULT_TIMEOUT) -> ProbeResult:
    """Fetch one URL and classify the outcome."""
    started = time.monotonic()
    req = urllib.request.Request(spec.url, headers={"User-Agent": UA})

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            elapsed = time.monotonic() - started
            text = raw.decode("utf-8", errors="replace")
            head = text[:4096]
            final = resp.geturl()
            status = resp.status

    except urllib.error.HTTPError as e:
        return _http_error(spec, e, time.monotonic() - started)
    except urllib.error.URLError as e:
        return _url_error(spec, e, time.monotonic() - started)
    except socket.timeout:
        return ProbeResult(
            spec, False, seconds=time.monotonic() - started,
            cause="TIMEOUT",
            detail=f"no response within {timeout}s",
            hint="The host may be slow, rate limiting, or silently dropping "
                 "requests from this IP. Retry; if it only fails from CI, "
                 "the runner's datacentre IP range may be blocked.")
    except Exception as e:  # noqa: BLE001 - want the class name in the report
        return ProbeResult(
            spec, False, seconds=time.monotonic() - started,
            cause="UNEXPECTED",
            detail=f"{type(e).__name__}: {e}",
            hint="Unclassified error — check the URL is well-formed and the "
                 "host is reachable from this network.")

    result = ProbeResult(spec, True, status=status, bytes_read=len(raw),
                         seconds=elapsed, final_url=final, body_head=head)

    # HTTP 200 does not mean we got what we asked for.
    if final.rstrip("/") != spec.url.rstrip("/"):
        looks_like_gate = any(
            w in final.lower() for w in ("login", "signin", "auth", "register"))
        if looks_like_gate:
            result.ok = False
            result.cause = "REDIRECTED TO GATE"
            result.detail = f"ended at {final}"
            result.hint = ("The source now requires authentication. Add "
                           "credentials to the source's required_secrets and "
                           "send them from harvest().")
            return result
        result.detail = f"redirected to {final}"

    if len(raw) < spec.min_bytes:
        result.ok = False
        result.cause = "BODY TOO SMALL"
        result.detail = f"{len(raw)} bytes, expected at least {spec.min_bytes}"
        result.hint = ("The endpoint responded but returned little or nothing. "
                       "Often a placeholder or error page. Open the URL in a "
                       "browser to compare.")
        return result

    stripped = head.lstrip()
    if spec.expect_prefix and not stripped.startswith(spec.expect_prefix):
        result.ok = False
        result.cause = "UNEXPECTED FORMAT"
        result.detail = (f"expected content starting {spec.expect_prefix!r}, "
                         f"got {stripped[:40]!r}")
        result.hint = ("The endpoint returned something other than the "
                       "expected payload — commonly an HTML error page, or the "
                       "upstream changed format. Check the source's version "
                       "pinning.")
        return result

    if spec.expect_contains and spec.expect_contains not in head:
        result.ok = False
        result.cause = "MARKER MISSING"
        result.detail = f"{spec.expect_contains!r} not found in first 4 KB"
        result.hint = ("Payload reachable but its shape has changed. Compare "
                       "against the live file before adjusting the parser.")
        return result

    return result


def _http_error(spec, e, elapsed) -> ProbeResult:
    status = e.code
    table = {
        401: ("UNAUTHORISED",
              "The source needs credentials, or the ones supplied were "
              "rejected. Check the source's required_secrets are set and "
              "have not expired."),
        403: ("FORBIDDEN",
              "Reachable but access refused. Either credentials lack "
              "permission, or the client is being blocked — bot protection "
              "commonly rejects datacentre IPs, so this may pass locally and "
              "fail in CI."),
        404: ("NOT FOUND",
              "The URL no longer exists. Most often the upstream published a "
              "new version: check the version pinning at the top of the "
              "source module."),
        410: ("GONE", "The upstream has permanently removed this resource."),
        429: ("RATE LIMITED",
              "Too many requests. Space out the schedule, or reduce how many "
              "endpoints the harvest touches per run."),
    }
    if status in table:
        cause, hint = table[status]
    elif 500 <= status < 600:
        cause = f"UPSTREAM ERROR {status}"
        hint = ("The source's own server failed. Usually transient — retry "
                "before changing anything.")
    else:
        cause = f"HTTP {status}"
        hint = "Unexpected status code; open the URL in a browser to compare."
    return ProbeResult(spec, False, status=status, seconds=elapsed,
                       cause=cause, detail=e.reason or "", hint=hint)


def _url_error(spec, e, elapsed) -> ProbeResult:
    reason = e.reason
    if isinstance(reason, socket.gaierror):
        return ProbeResult(
            spec, False, seconds=elapsed, cause="DNS FAILURE",
            detail=str(reason),
            hint="The hostname did not resolve. Check for a typo in the URL, "
                 "or whether the domain still exists.")
    if isinstance(reason, ssl.SSLError):
        return ProbeResult(
            spec, False, seconds=elapsed, cause="TLS FAILURE",
            detail=str(reason),
            hint="Certificate or TLS negotiation failed. If this only happens "
                 "on a corporate network, an inspecting proxy is likely.")
    if isinstance(reason, socket.timeout) or "timed out" in str(reason).lower():
        return ProbeResult(
            spec, False, seconds=elapsed, cause="TIMEOUT", detail=str(reason),
            hint="No response in time. Retry; persistent timeouts from CI but "
                 "not locally suggest the runner's IP range is blocked.")
    if isinstance(reason, ConnectionRefusedError):
        return ProbeResult(
            spec, False, seconds=elapsed, cause="CONNECTION REFUSED",
            detail=str(reason),
            hint="Host resolved but refused the connection. Check the scheme "
                 "and port, and whether the service is running.")
    return ProbeResult(
        spec, False, seconds=elapsed, cause="NETWORK ERROR", detail=str(reason),
        hint="Could not establish a connection. Check network egress rules if "
             "this is running behind a firewall or proxy.")


def probe_all(specs: list[ProbeSpec], timeout: int = DEFAULT_TIMEOUT
              ) -> list[ProbeResult]:
    return [probe(s, timeout=timeout) for s in specs]
