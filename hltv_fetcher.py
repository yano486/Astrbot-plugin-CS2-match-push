"""Low-frequency HLTV transport with standard HTTP and optional Chromium.

External I/O is injectable; caching, validation, redirect guards, throttling and
lifecycle remain real in offline tests. No browser or network starts on import.
"""

from __future__ import annotations

import asyncio
import aiohttp
import hashlib
import json
import math
import os
import platform
import re
import secrets
import time
import weakref
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from .hltv_parser import BASE, ParseError, allowed_url, is_challenge, parse_upcoming_matches

MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
_REDIRECTS = {301, 302, 303, 307, 308}
_PROXY_KEYS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")


@dataclass(frozen=True)
class FetchResponse:
    status: int
    url: str
    headers: dict
    body: bytes


@dataclass(frozen=True)
class Document:
    text: str
    url: str
    fetched_at: str
    cached: bool = False


class _Failure(Exception):
    def __init__(self, state, message, *, backoff=False):
        super().__init__(message)
        self.state, self.message, self.backoff = state, message, backoff


def _number(config, key, default, maximum):
    value = float(config.get(key, default))
    if not math.isfinite(value) or value < 0 or value > maximum:
        raise ValueError(f"Invalid {key}")
    return value


def _proxy(config):
    value = str(config.get("hltv_proxy_url") or "").strip()
    if not value:
        value = next((os.environ[k].strip() for k in _PROXY_KEYS if os.environ.get(k, "").strip()), "")
    if not value:
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https", "socks5", "socks5h"} or not parts.hostname or parts.port == 0:
            raise ValueError
        if parts.path not in {"", "/"} or parts.query or parts.fragment or any(ord(c) <= 32 for c in value):
            raise ValueError
    except ValueError:
        raise _Failure("error", "Invalid HLTV proxy configuration") from None
    return value


def _browser_proxy(value):
    if not value:
        return None
    parts = urlsplit(value)
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    port = parts.port or {"http": 80, "https": 443, "socks5": 1080, "socks5h": 1080}[parts.scheme]
    scheme = "socks5" if parts.scheme == "socks5h" else parts.scheme
    result = {"server": urlunsplit((scheme, f"{host}:{port}", "", "", ""))}
    if parts.username is not None:
        result["username"] = unquote(parts.username)
        result["password"] = unquote(parts.password or "")
    return result


def _state_path(config):
    directory = config.get("hltv_data_dir")
    if directory is None:
        from astrbot.api.star import StarTools
        directory = StarTools.get_data_dir("astrbot_plugin_cs_match")
    # Credentials belong to this device, not to portable plugin/config exports.
    device = hashlib.sha256(f"{platform.node()}|{platform.system()}|{Path.home()}".encode()).hexdigest()[:16]
    return Path(directory) / "hltv" / f"browser-state-{device}.json"


def _read_state(config):
    path = _state_path(config)
    if not path.exists():
        return None
    try:
        if path.stat().st_size > 1024 * 1024:
            raise ValueError
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not isinstance(state.get("cookies"), list) or not isinstance(state.get("origins"), list):
            raise ValueError
        for cookie in state["cookies"]:
            if not isinstance(cookie, dict) or cookie.get("domain", "").lstrip(".") not in {"hltv.org", "www.hltv.org"}:
                raise ValueError
        if any(not isinstance(origin, dict) or not allowed_url(origin.get("origin", "")) for origin in state["origins"]):
            raise ValueError
        return state
    except (OSError, ValueError, TypeError):
        raise _Failure("error", "HLTV browser state is unreadable; inspect the local state file") from None


def _write_state(config, state):
    path = _state_path(config)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Do not retain unrelated third-party cookies collected by subresources.
    state = {"cookies": [c for c in state.get("cookies", []) if c.get("domain", "").lstrip(".") in {"hltv.org", "www.hltv.org"}],
             "origins": [o for o in state.get("origins", []) if allowed_url(o.get("origin", ""))]}
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=True)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class _Gate:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.last_at = None
        self.last_gap = 0.0

    async def wait(self, clock, sleep, gap):
        if self.last_at is not None:
            delay = self.last_at + max(self.last_gap, gap) - clock()
            if delay > 0:
                await sleep(delay)
        self.last_at, self.last_gap = clock(), gap


_GATES = weakref.WeakKeyDictionary()


def _gate():
    loop = asyncio.get_running_loop()
    if loop not in _GATES:
        _GATES[loop] = _Gate()
    return _GATES[loop]


class _Chromium:
    """One shared browser/context; only explicit manual verification waits."""

    def __init__(self, config):
        self.config = config
        self.pw = self.browser = self.context = None
        self.headful = None
        self.uses = 0
        self.started_at = 0.0
        self.route_failure = None
        self.document_url = None

    async def _ensure(self, headful):
        if self.context is not None and (headful != self.headful or self.uses >= 100 or time.monotonic() - self.started_at >= 3600):
            await self.close()
        if self.context is not None:
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise _Failure("unavailable", "Playwright is not installed; Chromium fallback is unavailable") from None
        state = _read_state(self.config)
        proxy = _browser_proxy(_proxy(self.config))
        try:
            self.pw = await async_playwright().start()
            self.browser = await self.pw.chromium.launch(headless=not headful, proxy=proxy)
            self.context = await self.browser.new_context(
                storage_state=state, locale="en-US", viewport={"width": 1366, "height": 900}, service_workers="block"
            )
            await self.context.route("**/*", self._guard_route)
            self.headful, self.uses, self.started_at = headful, 0, time.monotonic()
        except BaseException:
            await self.close()
            raise

    async def _guard_route(self, route):
        request = route.request
        # Every request, including scripts, frames and subresources, stays on
        # the HLTV page/image allowlist; otherwise a page could reach localhost.
        if not allowed_url(request.url, asset=True):
            self.route_failure = _Failure("error", "Blocked non-HLTV browser request")
            await route.abort()
            return
        if not request.is_navigation_request() or request.frame.parent_frame is not None:
            await route.continue_()
            return
        url, method, post_data = request.url, request.method, request.post_data
        try:
            # route.continue_() follows HTTP redirects before another route
            # callback on some Chromium versions. Fetch each hop explicitly.
            for _ in range(6):
                if not allowed_url(url):
                    raise _Failure("error", "Blocked non-HLTV browser navigation")
                kwargs = {"url": url, "method": method, "max_redirects": 0, "timeout": 25000}
                if method not in {"GET", "HEAD"}:
                    kwargs["post_data"] = post_data
                response = await route.fetch(**kwargs)
                try:
                    if not allowed_url(response.url):
                        raise _Failure("error", "Blocked non-HLTV browser redirect")
                    if response.status in _REDIRECTS:
                        location = response.headers.get("location")
                        if not location:
                            raise _Failure("error", "Invalid HLTV browser redirect")
                        url = urljoin(url, location)
                        if response.status == 303 or (response.status in {301, 302} and method == "POST"):
                            method, post_data = "GET", None
                        continue
                    length = response.headers.get("content-length", "")
                    if length.isdecimal() and int(length) > MAX_DOCUMENT_BYTES:
                        raise _Failure("error", "HLTV browser document exceeds size limit")
                    body = await response.body()
                    if len(body) > MAX_DOCUMENT_BYTES:
                        raise _Failure("error", "HLTV browser document exceeds size limit")
                    self.document_url = response.url
                    await route.fulfill(response=response, body=body)
                    return
                finally:
                    await response.dispose()
            raise _Failure("error", "Too many HLTV browser redirects")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.route_failure = _Failure("error", "HLTV browser navigation was blocked or failed")
            await route.abort()

    async def _save(self):
        if self.context is not None:
            try:
                _write_state(self.config, await self.context.storage_state())
            except (OSError, ValueError, TypeError):
                raise _Failure("error", "Could not persist local HLTV browser state") from None

    async def get(self, url, *, max_bytes, headful=False, manual=False, kind="html"):
        await self._ensure(headful)
        self.uses += 1
        if kind == "image":
            return await self._asset(url, max_bytes)
        self.route_failure = None
        self.document_url = None
        page = await self.context.new_page()
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            if self.route_failure:
                raise self.route_failure
            if not allowed_url(page.url) or response is None:
                raise _Failure("error", "Invalid HLTV browser destination")
            if manual:
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    if not allowed_url(page.url):
                        raise _Failure("error", "Blocked non-HLTV verification destination")
                    text = await page.content()
                    try:
                        parse_upcoming_matches(text)
                    except ParseError:
                        # Observe only. The human operates the challenge UI.
                        await asyncio.sleep(2)
                        continue
                    await self._save()
                    return FetchResponse(200, page.url, {"content-type": "text/html"}, text.encode())
                raise _Failure("blocked", "Human verification timed out; HLTV still requires verification", backoff=True)
            headers = dict(response.headers)
            body = (await page.content()).encode() if kind == "html" else await response.body()
            if len(body) > max_bytes:
                raise _Failure("error", "HLTV browser response exceeds size limit")
            if response.status == 200 and not is_challenge(body.decode("utf-8", errors="replace")):
                await self._save()
            return FetchResponse(response.status, self.document_url or page.url, headers, body)
        finally:
            await page.close()

    async def _asset(self, url, max_bytes):
        # Images use the existing context's cookie/proxy-aware request client,
        # never a top-level navigation to an asset host.
        for _ in range(6):
            if not allowed_url(url, asset=True):
                raise _Failure("error", "Blocked non-HLTV asset redirect")
            response = await self.context.request.get(url, max_redirects=0, timeout=25000)
            try:
                if not allowed_url(response.url, asset=True):
                    raise _Failure("error", "Blocked non-HLTV asset destination")
                headers = dict(response.headers)
                if response.status in _REDIRECTS:
                    if not headers.get("location"):
                        raise _Failure("error", "Invalid HLTV asset redirect")
                    url = urljoin(url, headers["location"])
                    continue
                length = headers.get("content-length", "")
                if length.isdecimal() and int(length) > max_bytes:
                    raise _Failure("error", "HLTV asset exceeds size limit")
                body = await response.body()
                if len(body) > max_bytes:
                    raise _Failure("error", "HLTV asset exceeds size limit")
                return FetchResponse(response.status, response.url, headers, body)
            finally:
                await response.dispose()
        raise _Failure("error", "Too many HLTV asset redirects")

    async def close(self):
        context, browser, pw = self.context, self.browser, self.pw
        self.context = self.browser = self.pw = None
        # Every resource is attempted even if an earlier close fails.
        try:
            if context is not None:
                await context.close()
        finally:
            try:
                if browser is not None:
                    await browser.close()
            finally:
                if pw is not None:
                    await pw.stop()


class HltvFetcher:
    def __init__(self, config: dict, *, transport=None, browser_factory=None, clock=None, sleep=None):
        self.config = dict(config)
        self.min_gap = _number(config, "hltv_request_min_gap", 90, 86400)
        self.cache_ttl = _number(config, "hltv_cache_ttl", 180, 86400)
        self.enabled = config.get("data_source_mode", "hltv") in {"hltv", "both"}
        self._transport = transport
        self._browser_factory = browser_factory or _Chromium
        self._browser = self._session = None
        self._browser_verified = False
        self._clock, self._sleep = clock or time.monotonic, sleep or asyncio.sleep
        self._cache = OrderedDict()
        self._tasks = set()
        self._closed = False
        self._close_lock = asyncio.Lock()
        self._verifying = False
        self._until, self._failures = 0.0, 0
        self._state, self._message, self._last_success = "idle", "HLTV has not been checked", None

    def status(self):
        return {"enabled": self.enabled, "state": self._state, "message": self._message, "last_success": self._last_success}

    def _failure(self, failure):
        if self._closed:
            return
        self._state, self._message = failure.state, failure.message
        if failure.backoff:
            self._failures += 1
            base = 300 if failure.state == "blocked" else 30
            self._until = self._clock() + min(3600, base * 2 ** min(self._failures - 1, 7))

    def _success(self, at=None):
        if not self._closed:
            self._state, self._message = "ok", "HLTV response validated"
            self._last_success = at or datetime.now(timezone.utc).isoformat()
            self._failures, self._until = 0, 0.0

    async def _standard_http(self, url, *, headers, proxy, max_bytes):
        if self._session is None:
            try:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=25), trust_env=False,
                    cookie_jar=aiohttp.CookieJar(unsafe=False),
                )
            except Exception:
                raise _Failure("unavailable", "Standard HTTP transport is unavailable") from None
        response = None
        try:
            response = await self._session.get(url, headers=headers, proxy=proxy,
                                               allow_redirects=False, timeout=25)
            response_headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
            length = response_headers.get("content-length", "")
            if length.isdecimal() and int(length) > max_bytes:
                raise _Failure("error", "HLTV response exceeds size limit")
            chunks, total = [], 0
            async for chunk in response.content.iter_chunked(64 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise _Failure("error", "HLTV response exceeds size limit")
                chunks.append(chunk)
            return FetchResponse(response.status, str(response.url), response_headers, b"".join(chunks))
        except asyncio.CancelledError:
            raise
        except _Failure:
            raise
        except Exception:
            # aiohttp exceptions can include credential-bearing proxy URLs.
            raise _Failure("error", "HLTV HTTP transport failed", backoff=True) from None
        finally:
            if response is not None:
                response.release()

    async def _http(self, url, *, kind, max_bytes, gate):
        proxy = _proxy(self.config)
        headers = {"Accept": "application/json" if kind == "json" else "image/*" if kind == "image" else "text/html,application/xhtml+xml",
                   "Accept-Language": "en-US,en;q=0.9", "Referer": f"{BASE}/matches"}
        if kind == "json":
            headers["X-Requested-With"] = "XMLHttpRequest"
        transport = self._transport or self._standard_http
        for _ in range(6):
            if not allowed_url(url, asset=kind == "image"):
                raise _Failure("error", "Blocked non-HLTV URL or redirect")
            await gate.wait(self._clock, self._sleep, self.min_gap)
            response = await transport(url, headers=headers, proxy=proxy, max_bytes=max_bytes)
            if not allowed_url(response.url, asset=kind == "image"):
                raise _Failure("error", "Blocked non-HLTV response destination")
            if response.status not in _REDIRECTS:
                return response
            location = next((v for k, v in response.headers.items() if k.lower() == "location"), None)
            if not location:
                raise _Failure("error", "Invalid HLTV redirect")
            url = urljoin(url, location)
        raise _Failure("error", "Too many HLTV redirects")

    @staticmethod
    def _validate(response, kind, max_bytes):
        if not allowed_url(response.url, asset=kind == "image"):
            raise _Failure("error", "Blocked non-HLTV response destination")
        if not isinstance(response.body, bytes) or len(response.body) > max_bytes:
            raise _Failure("error", "HLTV response exceeds size limit")
        text = response.body.decode("utf-8", errors="replace")
        if is_challenge(text) or response.status in {403, 429}:
            raise _Failure("blocked", "HLTV requires human verification or is rate limited", backoff=True)
        if response.status != 200:
            raise _Failure("error", f"HLTV returned HTTP {int(response.status)}", backoff=response.status >= 500)
        content_type = next((str(v).lower().split(";", 1)[0].strip() for k, v in response.headers.items() if k.lower() == "content-type"), "")
        valid_type = content_type.startswith("image/") if kind == "image" else content_type in (
            {"application/json"} if kind == "json" else {"text/html", "application/xhtml+xml"}
        )
        if not valid_type or not response.body:
            raise _Failure("error", "HLTV returned an unexpected content type or empty response")
        return text

    async def _browser_response(self, url, *, kind, max_bytes, gate):
        if self._browser is None:
            self._browser = self._browser_factory(self.config)
        await gate.wait(self._clock, self._sleep, self.min_gap)
        response = await self._browser.get(url, kind=kind, max_bytes=max_bytes,
                                           headful=bool(self.config.get("hltv_headful", False)))
        self._validate(response, kind, max_bytes)
        return response

    async def _response(self, url, *, kind, max_bytes, gate):
        # Once an administrator has completed verification, keep using the
        # browser context; standard HTTP cannot carry its browser challenge state.
        if self._browser_verified:
            return await self._browser_response(url, kind=kind, max_bytes=max_bytes, gate=gate)
        try:
            response = await self._http(url, kind=kind, max_bytes=max_bytes, gate=gate)
            self._validate(response, kind, max_bytes)
            return response
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = exc if isinstance(exc, _Failure) else _Failure("error", "HLTV HTTP transport failed", backoff=True)
            # A blocked redirect is a security failure, not a reason to send
            # the same navigation through a second channel.
            # A challenge/rate-limit is an explicit failure. Do not silently
            # attempt another channel; human verification is administrator-only.
            fallback = (failure.state == "unavailable"
                        or failure.message == "HLTV HTTP transport failed"
                        or (failure.message.startswith("HLTV returned HTTP ") and failure.backoff))
            if not fallback or not self.config.get("hltv_browser_fallback", True):
                raise failure from None
            try:
                return await self._browser_response(url, kind=kind, max_bytes=max_bytes, gate=gate)
            except asyncio.CancelledError:
                raise
            except _Failure as browser_failure:
                # Preserve the challenge state when both channels are blocked
                # or the optional browser is unavailable.
                if failure.state == "blocked" and browser_failure.state == "unavailable":
                    raise failure from None
                raise
            except Exception:
                raise _Failure("error", "Chromium is unavailable or failed; check its local installation/display", backoff=True) from None

    def _cached_document(self, key, validator):
        cached = self._cache.get(key)
        if cached is None:
            return None
        if self._clock() - cached[0] >= self.cache_ttl:
            self._cache.pop(key)
            return None
        if validator:
            validator(cached[1].text)
        self._cache.move_to_end(key)
        return replace(cached[1], cached=True)

    async def fetch(self, url, *, kind="html", validator=None):
        if self._closed or not self.enabled:
            return None
        if kind not in {"html", "json"} or not allowed_url(url):
            self._failure(_Failure("error", "Blocked non-HLTV page URL"))
            return None
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            key = (kind, url)
            cached = self._cached_document(key, validator)
            if cached is not None:
                return cached
            if self._verifying:
                return None
            gate = _gate()
            async with gate.lock:
                if self._closed or self._verifying:
                    return None
                cached = self._cached_document(key, validator)
                if cached is not None:
                    return cached
                if self._clock() < self._until:
                    return None
                response = await self._response(url, kind=kind, max_bytes=MAX_DOCUMENT_BYTES, gate=gate)
                expected = re.match(r"/(matches|team)/([0-9]+)(?:/|$)", urlsplit(url).path)
                actual = re.match(r"/(matches|team)/([0-9]+)(?:/|$)", urlsplit(response.url).path)
                if expected and (not actual or expected.groups() != actual.groups()):
                    raise _Failure("error", "HLTV redirected to a different record; response rejected")
                text = self._validate(response, kind, MAX_DOCUMENT_BYTES)
                if validator:
                    validator(text)
                at = datetime.now(timezone.utc).isoformat()
                document = Document(text, response.url, at)
                # Only parsed/validated documents (including real empty lists)
                # are cacheable. A raw HTTP 200 is not evidence of a good page.
                if validator is not None and self.cache_ttl > 0:
                    self._cache[key] = (self._clock(), document)
                    while len(self._cache) > 32:
                        self._cache.popitem(last=False)
                self._success(at)
                return document
        except asyncio.CancelledError:
            raise
        except ParseError:
            self._failure(_Failure("error", "HLTV page structure is not recognized; no data was cached"))
            return None
        except _Failure as failure:
            self._failure(failure)
            return None
        finally:
            self._tasks.discard(task)

    async def fetch_bytes(self, url, max_bytes=2 * 1024 * 1024):
        if self._closed or not self.enabled or self._verifying:
            return None
        if not allowed_url(url, asset=True) or isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            return None
        max_bytes = min(max_bytes, 16 * 1024 * 1024)
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            gate = _gate()
            async with gate.lock:
                if self._closed or self._clock() < self._until:
                    return None
                response = await self._response(url, kind="image", max_bytes=max_bytes, gate=gate)
                return response.body
        except asyncio.CancelledError:
            raise
        except _Failure as failure:
            self._failure(failure)
            return None
        finally:
            self._tasks.discard(task)

    async def manual_verify(self):
        if self._closed or not self.enabled or self._verifying:
            return {"ok": False, "message": "HLTV verification is disabled, closing, or already active"}
        task = asyncio.current_task()
        self._tasks.add(task)
        self._verifying = True
        self._state, self._message = "verifying", "Complete verification in the local headed browser (up to 180 seconds)"
        try:
            gate = _gate()
            async with gate.lock:
                if self._closed:
                    return {"ok": False, "message": "HLTV is closed"}
                if self._browser is None:
                    try:
                        self._browser = self._browser_factory(self.config)
                    except Exception:
                        raise _Failure("unavailable", "Could not initialize Chromium; check its local installation/display") from None
                await gate.wait(self._clock, self._sleep, self.min_gap)
                response = await self._browser.get(f"{BASE}/matches", max_bytes=MAX_DOCUMENT_BYTES,
                                                   headful=True, manual=True)
                text = self._validate(response, "html", MAX_DOCUMENT_BYTES)
                parse_upcoming_matches(text)
                self._cache.clear()
                self._browser_verified = True
                self._success()
                return {"ok": True, "message": "HLTV human verification completed; browser state is stored locally"}
        except asyncio.CancelledError:
            if not self._closed:
                self._failure(_Failure("blocked", "HLTV human verification was canceled"))
            raise
        except (ParseError, _Failure) as exc:
            failure = exc if isinstance(exc, _Failure) else _Failure("blocked", "HLTV still requires human verification", backoff=True)
            self._failure(failure)
            return {"ok": False, "message": failure.message}
        except Exception:
            failure = _Failure("unavailable", "Could not open Chromium; check its installation and local display")
            self._failure(failure)
            return {"ok": False, "message": failure.message}
        finally:
            self._verifying = False
            self._tasks.discard(task)

    async def close(self):
        async with self._close_lock:
            if self._closed and self._browser is None and self._session is None and not self._tasks:
                return
            self._closed = True
            self._state, self._message = "closed", "HLTV client is closed"
            tasks = [t for t in self._tasks if t is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            try:
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                self._cache.clear()
                resources = (self._browser, self._session, self._transport)
                self._browser = self._session = None
                errors = []
                for resource in resources:
                    if resource is not None and hasattr(resource, "close"):
                        try:
                            await resource.close()
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            errors.append("HLTV resource cleanup failed")
                if errors:
                    self._message = "HLTV closed with a resource cleanup error"
                    raise RuntimeError(self._message) from None
