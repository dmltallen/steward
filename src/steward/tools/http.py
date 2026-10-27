"""http tool: fetch one URL, net risk, allowlist only.

http.fetch contacts exactly the https hosts named in settings.http_allowlist:
the scheme must be https, credentials are refused, and the host must match an
allowlist entry exactly. A redirect is re-checked against the same policy
before it is followed, so a chain can never hop onto a host that was not
allowlisted. Bodies are capped.

Risk: net.
"""

from __future__ import annotations

import ssl
import urllib.error
import urllib.parse
import urllib.request

from steward.tools.sandbox import ToolError, check_url_policy

_DEFAULT_TIMEOUT = 8.0


class _AllowlistRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow a redirect whose target fails the fetch policy."""

    allowlist: list = []  # populated per opener with the configured hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            check_url_policy(newurl, self.allowlist)
        except ToolError as exc:
            raise ToolError("redirect refused: %s" % exc)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener(settings) -> urllib.request.OpenerDirector:
    context = settings.http_tls_context
    if context is None:
        context = ssl.create_default_context()  # real TLS verification
    redirect = _AllowlistRedirect()
    redirect.allowlist = list(settings.http_allowlist)
    https = urllib.request.HTTPSHandler(context=context)
    return urllib.request.build_opener(redirect, https)


def preflight_url(settings, args: dict) -> None:
    """Reject a fetch target before an approval is even prompted.

    https-only and exact-allowlist refusals surface here (fail fast) rather
    than creating an approval row for a request that policy must reject.
    """
    check_url_policy(args["url"], settings.http_allowlist)


def http_fetch(settings, args: dict) -> dict:
    url = str(args["url"])
    check_url_policy(url, settings.http_allowlist)
    timeout = float(args.get("timeout") or settings.http_timeout_s or _DEFAULT_TIMEOUT)
    cap = max(1, int(settings.max_http_bytes))
    try:
        with _opener(settings).open(url, timeout=timeout) as resp:
            body = resp.read(cap + 1)
        truncated = len(body) > cap
        body = body[:cap]
        return {
            "url": url,
            "status": resp.status,
            "content_type": resp.headers.get("Content-Type"),
            "bytes": len(body),
            "truncated": truncated,
            "body": body.decode("utf-8", errors="replace"),
        }
    except urllib.error.HTTPError as exc:
        return {"url": url, "status": exc.code,
                "content_type": exc.headers.get("Content-Type") if exc.headers else None,
                "error": "%s %s" % (exc.code, exc.reason)}
    except urllib.error.URLError as exc:
        raise ToolError("fetch failed: %s" % (exc.reason or exc))
    except (TimeoutError, OSError) as exc:
        raise ToolError("fetch failed: %s" % exc)
