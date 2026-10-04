"""Lab client: a fixed allowlist, network checks, one span per attempt, and the two retry versions."""
import asyncio
import json
import re
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, build_opener

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

PATHS = {"/dns", "/portal", "/inventory/v1", "/inventory/v2", "/logs", "/deployment"}
INVENTORY_PATH = re.compile(r"/inventory(/[\w-]*)?")  # the inventory API; unknown paths get a real 404


def request(base, path, timeout):
    # No proxy, redirect, arbitrary URL, or external destination accepted by tools.
    from urllib.request import HTTPRedirectHandler
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args):
            return None
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        response = opener.open(base + path, timeout=timeout)
    except HTTPError as error:
        response = error
    with response:
        return response.status, dict(response.headers), json.loads(response.read(65536))


@dataclass
class LabClient:
    base: str
    retries: str  # "broken" (version 1) or "fixed" (version 2)
    deployment_found: bool | None = None  # set by read_deployment, read by the coordinator's eval

    async def check(self, path):
        """Health check: one request, because a failure is the answer."""
        status, _, body = await self._get(path, attempt=1)
        return {"http_status": status, "data": body}

    async def fetch(self, path):
        """Evidence lookup, using this run's retry version."""
        if self.retries == "broken":
            # Version 1: if anything goes wrong, try again right away, up to five times.
            for attempt in range(1, 6):
                status, headers, body = await self._get(path, attempt)
                if status == 200:
                    return {"available": True, "attempts": attempt, "data": body}
        else:
            # Version 2: on a 429, wait as long as Retry-After asks, then try one more time.
            for attempt in range(1, 3):
                status, headers, body = await self._get(path, attempt)
                if status == 200:
                    return {"available": True, "attempts": attempt, "data": body}
                if status != 429 or attempt == 2:
                    break
                await asyncio.sleep(min(float(headers.get("Retry-After", 1)), 5))
        # The tool returns normally, so mark its span as failed or the timeline shows a success.
        tool_span = trace.get_current_span()
        tool_span.set_status(Status(StatusCode.ERROR, "Evidence unavailable"))
        tool_span.set_attribute("error.type", "evidence_unavailable")
        return {"available": False, "http_status": status, "attempts": attempt, "evidence_id": path,
                "reason": "Evidence unavailable; do not infer its contents."}

    async def dns(self, hostname):
        """Look up a hostname in the lab's authored DNS records."""
        records = (await self.check("/dns"))["data"]
        if hostname not in records:
            return {"hostname": hostname, "found": False, "error": "NXDOMAIN"}
        return {"hostname": hostname, "found": True, "address": records[hostname], "evidence_id": records["evidence_id"]}

    async def tcp(self, hostname, port):
        """Port check: open and close a real TCP connection to the lab server."""
        if not (await self.dns(hostname))["found"]:
            return {"hostname": hostname, "port": port, "open": False, "error": "could not resolve host"}
        with trace.get_tracer(__name__).start_as_current_span(f"TCP {hostname}:{port}", kind=trace.SpanKind.CLIENT) as span:
            span.set_attributes({"server.address": hostname, "server.port": port, "network.transport": "tcp"})
            if port not in (80, 443):  # Lab hosts serve web traffic on 80 and 443; other ports refuse by design.
                span.set_status(Status(StatusCode.ERROR))
                span.set_attribute("error.type", "connection_refused")
                return {"hostname": hostname, "port": port, "open": False, "error": "connection refused"}
            start = time.monotonic()
            server = urlparse(self.base)
            _, writer = await asyncio.wait_for(asyncio.open_connection(server.hostname, server.port), 2)
            writer.close()
            await writer.wait_closed()
            return {"hostname": hostname, "port": port, "open": True, "connect_ms": round((time.monotonic() - start) * 1000, 1)}

    async def http(self, url):
        """One HTTP GET to a lab URL. The network agent can reach the portal and the inventory API only."""
        parts = urlparse(url)
        host, path = parts.hostname or "", parts.path or "/"
        if host not in ("portal.lab", "inventory.lab"):
            return {"url": url, "error": "could not resolve host"}
        if host == "portal.lab" and path in ("/", "/portal"):
            path = "/portal"
        elif not (host == "inventory.lab" and INVENTORY_PATH.fullmatch(path)):
            return {"url": url, "error": "this lab only serves http://portal.lab/portal and http://inventory.lab/inventory/..."}
        return {"url": url, **await self.check(path)}

    async def _get(self, path, attempt):
        if path not in PATHS and not INVENTORY_PATH.fullmatch(path):
            raise ValueError("Unknown lab endpoint")
        with trace.get_tracer(__name__).start_as_current_span("GET " + path, kind=trace.SpanKind.CLIENT) as span:
            span.set_attributes({"http.request.method": "GET", "url.path": path, "lab.http.attempt": attempt})
            try:
                status, headers, body = await asyncio.to_thread(request, self.base, path, 2)
            except (URLError, TimeoutError, OSError) as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR))
                return 0, {}, {"error": type(exc).__name__}
            span.set_attribute("http.response.status_code", status)
            if "Retry-After" in headers:
                span.set_attribute("http.response.header.retry-after", [headers["Retry-After"]])
            if status >= 400:
                span.set_status(Status(StatusCode.ERROR))
                span.set_attribute("error.type", str(status))
            return status, headers, body
