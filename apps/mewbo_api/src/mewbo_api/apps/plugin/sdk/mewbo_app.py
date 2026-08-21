"""mewbo_app — the read-only data SDK injected into every Mewbo App's frontend.

This file is bundled into each app's stlite (Streamlit-in-WASM) kernel at RENDER
time, right beside the render context the host writes as ``_app_context.json``
(``{token, api_base, app_id}`` — see ``apps/mewbo_console/src/widget/stliteBoot.ts``
``buildAppKernelOptions``). It is the app's ONE sanctioned network path: app code
does ``import mewbo_app`` and reads its data + system namespaces through here, and
the app linter bans every raw HTTP client (`requests`/`httpx`/`urllib`/`js`) so no
other egress exists.

The SDK is READ-first: the served frontend reads business data (``data.query``)
and read-only introspection (``system`` / ``pipelines.list`` / ``pipelines.run``)
over GETs. The ONE write path is ``pipelines.submit(name, params)`` — a POST
that flows a frontend form's user input into a
``user_writable`` ``mode="code"`` pipeline. It needs a WRITE-scoped render token,
which the platform mints only for an app that DECLARES such a pipeline; the token's
scope arrives in the render context (``scope``, defaulting to ``"read"``), so the
SDK raises a clear client-side error on ``submit`` when the page holds only a read
token — never a silent 403.

Transport: a SYNCHRONOUS ``XMLHttpRequest`` (via pyodide's ``js`` bridge), because
Streamlit reruns are synchronous — an async ``pyfetch`` would need an event loop the
script doesn't own. The render-scoped token rides the ``X-Mewbo-App-Token`` header
when the page is SAME-origin with ``api_base`` (the console's own render path — no
CORS involved at all). When the embedding page is CROSS-origin (Aura's WebView,
which serves the stlite shell from its own scheme/host) the token instead rides the
``?token=`` query parameter the server already accepts alongside the header — this
SDK never depends on a CORS preflight approving the custom auth header, because a
GET with no custom headers needs no preflight in the first place. A ``submit`` POST
still sends its params as a JSON body (object/array values fully supported, unlike
the GET query-string path ``run`` uses); its ``Content-Type`` header still triggers
a preflight cross-origin, which is the API host's allow-headers list to satisfy,
not this SDK's problem to route around. The ``js`` import is LAZY (inside the
request method) so this module imports cleanly outside a browser too.

Token expiry surfaces as :class:`AppTokenExpired`, which an app should catch and
render as a calm "refresh the app" state, never a stack trace (the reference
``examples/email_organizer/app.py`` shows the pattern).
"""

from __future__ import annotations

import json
from typing import Any

_CONTEXT_FILE = "_app_context.json"
_TOKEN_HEADER = "X-Mewbo-App-Token"
_TOKEN_QUERY_PARAM = "token"
_PAGE_SIZE = 500


class AppError(RuntimeError):
    """Base class for every error this SDK raises."""


class AppContextMissing(AppError):
    """``_app_context.json`` was absent or malformed — the app wasn't mounted by the host."""


class AppTokenExpired(AppError):
    """The render token expired (HTTP 401/403). Prompt the user to refresh the app."""


class AppRequestError(AppError):
    """A data/system request failed for a reason other than an expired token.

    Also raised (wrapping the underlying transport exception) when the request
    never reached the server at all — a browser-level ``NetworkError`` a
    synchronous XHR raises synchronously, e.g. a CORS rejection or a TLS-trust
    failure in the embedding WebView — so an app never surfaces a raw pyodide
    ``JsException`` traceback. Never carries the render token.
    """


class MewboApp:
    """A live connection to this app's read-only data + system namespaces.

    Construct once per Streamlit run (or use :func:`connect`, which caches one):

    ```python
    import mewbo_app
    app = mewbo_app.connect()
    emails = app.data.query("emails", filter={"status": "unread"}, limit=50)
    triggers = app.system.triggers()
    ```

    Reads ``token`` / ``api_base`` / ``app_id`` from ``_app_context.json`` — the
    render context the host injects. Never construct these values yourself.
    """

    def __init__(self, context_file: str = _CONTEXT_FILE) -> None:
        """Load the render context and expose the ``data`` + ``system`` namespaces."""
        try:
            with open(context_file, encoding="utf-8") as handle:
                context = json.load(handle)
            self._token = str(context["token"])
            self._api_base = str(context["api_base"]).rstrip("/")
            self._app_id = str(context["app_id"])
            # The token's scope, injected by the host beside the token. Absent on
            # a context with no scope key ⇒ "read" (the only scope that ever
            # existed before write-back). Drives the client-side ``submit`` guard.
            self._scope = str(context.get("scope", "read"))
        except (OSError, KeyError, ValueError, TypeError) as exc:
            raise AppContextMissing(
                f"could not read {context_file}: {exc} — is the app mounted by the host?"
            ) from exc
        self.data = _AppData(self)
        self.system = _AppSystem(self)
        self.pipelines = _AppPipelines(self)

    @property
    def app_id(self) -> str:
        """This app's id."""
        return self._app_id

    def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        """Synchronous, token-authenticated GET of a same-origin app endpoint."""
        return self._send("GET", path, params=params)

    def _get_text(self, path: str, params: dict[str, str] | None = None) -> str:
        """Synchronous GET returning the response body unchanged, not JSON-decoded."""
        result = self._send("GET", path, params=params, parse_json=False)
        return result if isinstance(result, str) else ""

    def _post(self, path: str, body: Any) -> Any:
        """Synchronous, token-authenticated POST (JSON body) of a same-origin app endpoint.

        The write-back path (``pipelines.submit``): the JSON *body* carries the
        pipeline's params — object/array values fully supported, unlike the GET
        query-string path. Requires a WRITE-scoped render token (the server 403s a
        read token here), but ``submit`` pre-checks the scope client-side so a
        read-token page never reaches this call.
        """
        return self._send("POST", path, body=body)

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: Any = None,
        parse_json: bool = True,
    ) -> Any:
        """The shared XHR core behind :meth:`_get` / :meth:`_post`.

        A blocking ``XMLHttpRequest`` (composes with Streamlit's synchronous rerun
        model). Same-origin (the console's own render path, or an indeterminate
        non-browser context such as this test suite) rides the render token on the
        ``X-Mewbo-App-Token`` header, unchanged. Cross-origin (Aura's WebView) rides
        it on the ``?token=`` query parameter instead and never sets the custom
        header — a GET then triggers NO CORS preflight at all, so this SDK never
        depends on one being approved. A JSON *body* (POST) still sets
        ``Content-Type: application/json`` in both modes; that header still
        preflights cross-origin, which is the API host's allow-headers list to
        satisfy, not a concern this method routes around.

        Raises :class:`AppTokenExpired` on 401/403, :class:`AppRequestError` on any
        other non-2xx, and :class:`AppRequestError` (wrapping the cause) when the
        request never reached the server at all.
        """
        # Lazy import — only meaningful inside pyodide; keeps this module importable
        # (and unit-inspectable) in a plain Python process.
        from js import XMLHttpRequest

        display_url = f"{self._api_base}/api/apps/{self._app_id}/{path}"
        if params:
            display_url = f"{display_url}?{_encode_query(params)}"

        cross_origin = self._is_cross_origin()
        request_url = display_url
        if cross_origin:
            token_query = _encode_query({_TOKEN_QUERY_PARAM: self._token})
            request_url = f"{display_url}{'&' if '?' in display_url else '?'}{token_query}"

        request = XMLHttpRequest.new()
        request.open(method, request_url, False)  # False => synchronous
        if not cross_origin:
            request.setRequestHeader(_TOKEN_HEADER, self._token)
        payload = None
        if body is not None:
            request.setRequestHeader("Content-Type", "application/json")
            payload = json.dumps(body)
        try:
            request.send(payload)
            status = int(request.status)
            text = request.responseText
        except Exception as exc:  # the browser's opaque NetworkError (JsException)
            origin_note = f"cross-origin to {self._api_base}" if cross_origin else "same-origin"
            raise AppRequestError(
                f"request to {display_url} ({origin_note}) never reached the server: "
                f"{exc}. Likely causes: the API host's CORS policy is rejecting this "
                "origin, or the embedding WebView doesn't trust the API host's TLS "
                "certificate."
            ) from exc
        if status in (401, 403):
            raise AppTokenExpired("the app's access token has expired — refresh the app")
        if not 200 <= status < 300:
            raise AppRequestError(f"request to {path} failed ({status}): {text}")
        if not parse_json:
            return text
        return json.loads(text) if text else None

    def _is_cross_origin(self) -> bool:
        """True iff the embedding page's origin differs from the configured API origin.

        Same-origin, or an inability to determine the page origin at all (this
        module imported outside a browser, as in the test suite), both resolve to
        False — the pre-existing header-based transport, unchanged, is always safe
        there. Only a CONFIRMED mismatch switches the token to the query parameter.
        """
        try:
            from js import window  # pyodide only; absent outside a real browser

            page_origin = str(window.location.origin)
        except Exception:
            return False
        return page_origin.lower() != _origin_of(self._api_base).lower()


class _AppData:
    """The app's business-data namespace (``/api/apps/<id>/data/...``)."""

    def __init__(self, app: MewboApp) -> None:
        self._app = app

    def query(
        self,
        collection: str,
        *,
        filter: dict[str, Any] | None = None,  # noqa: A002 — matches the agent-side app_data arg
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Read documents from *collection* (optional equality *filter*, capped by *limit*).

        Pages transparently: the route caps a single page at ``_PAGE_SIZE``
        documents, so a *limit* above that is satisfied by fetching successive
        pages (via ``offset``) until *limit* is reached, the server reports no
        more documents (``truncated`` false), or a page comes back empty (the
        safety stop against an infinite loop).

        Returns a list of document BODIES (each the ``doc`` the pipeline wrote),
        with the storage envelope (``app_id``/``collection``/``key``/``updated_at``)
        unwrapped so app code renders its own schema directly.
        """
        rows: list[dict[str, Any]] = []
        offset = 0
        while len(rows) < limit:
            page_limit = min(_PAGE_SIZE, limit - len(rows))
            params: dict[str, str] = {"limit": str(page_limit), "offset": str(offset)}
            if filter:
                params["filter"] = json.dumps(filter, separators=(",", ":"))
            result = self._app._get(f"data/{collection}", params)
            page = _as_list(result, "documents")
            if not page:
                break
            rows.extend(page)
            offset += len(page)
            if not (isinstance(result, dict) and result.get("truncated")):
                break
        # The read route returns AppDataDoc envelopes ({app_id, collection, key,
        # doc, updated_at}); expose just the doc body. A row that is already bare
        # (no nested ``doc``) passes through unchanged.
        return [row["doc"] if isinstance(row.get("doc"), dict) else row for row in rows[:limit]]


class _AppPipelines:
    """The app's declared code-pipeline namespace.

    ``list`` / ``run`` / ``refresh`` ride the read-scoped render token — the same
    auth as ``data``/``system``. ``run`` invokes a declared `mode="code"` pipeline
    over a GET (scalar query params only) and returns its result. ``refresh``
    triggers an on-demand refresh of ANY pipeline (both modes) via the ``/fire``
    seam. ``submit`` is the ONE write path: a POST that
    flows a frontend form's user input into a `user_writable` pipeline, needing a
    WRITE-scoped token the platform mints only for an app that declares such a
    pipeline.
    """

    def __init__(self, app: MewboApp) -> None:
        self._app = app

    def list(self) -> list[dict[str, Any]]:
        """List this app's declared pipelines.

        Each row: ``{name, mode, tier, on_demand, schedule, armed, params_schema,
        cache_ttl_seconds}`` — a projection distinct from ``/system``'s
        pipeline rows (no ``trigger_ref``/``entrypoint``; those never cross
        the wire to a client).
        """
        return _as_list(self._app._get("pipelines"), "pipelines")

    def run(self, name: str, params: dict[str, Any] | None = None) -> Any:
        """Invoke a declared `mode="code"` pipeline by name; return its `output`.

        Each `params` entry becomes its OWN query-string key (the server reads
        `request.args` flat and coerces each declared property by its
        `params_schema` type) — there is NO wrapping `params=` key and no JSON
        blob; a `None` value is omitted rather than sent as the literal string
        `"None"`. **Known v1 gap (server-side, not just here):** an
        object/array-typed `params_schema` property can't be sent this way — a
        query string carries only scalars, coerced as integer/number/boolean/
        string; such a pipeline is only invocable via the write-scoped POST
        path, which this read-only SDK never uses (v1 has no write
        path here at all). A dict/list value is rejected HERE, client-side,
        before any network call — a `str()` of it would silently become a
        confusing repr string the server would then reject with a much less
        legible schema error. Raises :class:`AppRequestError` for that case,
        and :class:`AppTokenExpired`/:class:`AppRequestError` exactly like
        every other call here otherwise.
        """
        query: dict[str, str] = {}
        for key, value in (params or {}).items():
            if value is None:
                continue
            if isinstance(value, (dict, list)):
                raise AppRequestError(
                    f"params[{key!r}] is a {type(value).__name__} — object/array "
                    f"params can't be sent over this read-only GET path (v1 "
                    f"limitation); this pipeline needs the write-scoped POST path, "
                    f"which this SDK doesn't use"
                )
            query[key] = str(value)
        return self._app._get(f"pipelines/{name}", query or None)

    def result(self, name: str, params: dict[str, Any] | None = None) -> str:
        """Invoke a rendered pipeline and return its declared response body as text.

        This has the same scalar-query limitation as :meth:`run`: a dict/list
        value cannot cross the GET path and is refused client-side before any
        network call. The server returns the declared JSON/CSV/XML/text body
        directly, so this method intentionally does not JSON-decode it.
        """
        query: dict[str, str] = {}
        for key, value in (params or {}).items():
            if value is None:
                continue
            if isinstance(value, (dict, list)):
                raise AppRequestError(
                    f"params[{key!r}] is a {type(value).__name__} — object/array "
                    "params can't be sent over this read-only GET path (v1 limitation)"
                )
            query[key] = str(value)
        return self._app._get_text(f"pipelines/{name}/result", query or None)

    def refresh(self, name: str) -> dict[str, Any]:
        """Trigger an on-demand refresh of a pipeline (``POST .../pipelines/<name>/fire``).

        The on-demand counterpart to the platform's schedule — works for BOTH
        modes. A `mode="code"` pipeline runs synchronously and returns its result
        (`{pipeline, mode:"code", status:"succeeded", cache, docs_written,
        evaluated_at}`); a `mode="agentic"` pipeline wakes its maintainer and
        returns an acknowledgement (`{pipeline, mode:"agentic", status:"started"}`)
        — the refreshed data lands asynchronously (poll `system` / `data`).

        Rides the ambient render token (read auth suffices, same transport as every
        other call — no write scope needed, unlike :meth:`submit`). A 409 (app not
        live, or a run already in progress) or 429 (fired within the cooldown)
        surfaces as :class:`AppRequestError` carrying the server's message; token
        expiry as :class:`AppTokenExpired`.
        """
        result = self._app._post(f"pipelines/{name}/fire", {})
        return result if isinstance(result, dict) else {}

    def submit(self, name: str, params: dict[str, Any] | None = None) -> Any:
        """Submit user-input *params* to a `user_writable` `mode="code"` pipeline.

        Returns the pipeline's `output`. The write-back path (the write
        flows). Unlike :meth:`run` (a GET
        whose scalar params ride the query string), this POSTs *params* as the
        JSON request body, so object/array-typed properties are fully supported —
        a Streamlit `st.form` can flow its fields straight through the pipeline's
        `params_schema` validation into its `ctx.collection` writes.

        Needs a WRITE-scoped render token, which the platform mints only for an
        app that DECLARES a `user_writable` pipeline. When the page holds a read
        token (the default — this app declared no user-writable pipeline), this
        raises :class:`AppRequestError` client-side BEFORE any network call, with
        an actionable message, rather than letting the server 403. Otherwise
        raises :class:`AppTokenExpired`/:class:`AppRequestError` exactly like every
        other call here.
        """
        if self._app._scope != "write":
            raise AppRequestError(
                "app.pipelines.submit(...) needs a write-scoped app, but this page "
                "holds a read-only token: this app declares no user-writable "
                "pipeline. Declare `user_writable: true` on a mode='code' pipeline "
                "to enable form submission."
            )
        return self._app._post(f"pipelines/{name}", params or {})


class _AppSystem:
    """The app's read-only introspection namespace (``GET /api/apps/<id>/system``).

    One consolidated endpoint returns freshness + triggers + runs + maintainer.
    :meth:`health` fetches it; :meth:`triggers`/:meth:`runs`/:meth:`freshness` are
    convenience accessors over the same payload (call :meth:`health` once if you
    need several parts in one render).
    """

    def __init__(self, app: MewboApp) -> None:
        self._app = app

    def health(self) -> dict[str, Any]:
        """The full consolidated system snapshot (freshness, triggers, runs, maintainer)."""
        result = self._app._get("system")
        return result if isinstance(result, dict) else {}

    def triggers(self) -> list[dict[str, Any]]:
        """The app's triggers with their next fire time + last result (freshness pane)."""
        return _as_list(self.health(), "triggers")

    def runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Recent pipeline runs from the provenance ledger (newest first, capped)."""
        return _as_list(self.health(), "runs")[:limit]

    def freshness(self) -> Any:
        """The app's freshness signal (how recently a pipeline last succeeded)."""
        return self.health().get("freshness")


# ------------------------------------------------------------------
# Module-level convenience
# ------------------------------------------------------------------

_DEFAULT: MewboApp | None = None


def connect() -> MewboApp:
    """Return a cached :class:`MewboApp` for the current render (constructs one once)."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = MewboApp()
    return _DEFAULT


def _encode_query(params: dict[str, str]) -> str:
    """URL-encode a flat ``{str: str}`` mapping (stdlib string work, no network module)."""
    from urllib.parse import urlencode

    return urlencode(params)


def _origin_of(url: str) -> str:
    """Extract ``scheme://host[:port]`` from *url* (stdlib parse, no network module)."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _as_list(payload: Any, key: str) -> list[dict[str, Any]]:
    """Normalize a response to a list of dicts.

    Accepts either a bare JSON array or an envelope ``{<key>: [...]}`` (the shape
    the read routes return), so the SDK is tolerant of both response conventions.
    """
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        inner = payload.get(key)
        if isinstance(inner, list):
            return [row for row in inner if isinstance(row, dict)]
    return []


__all__ = [
    "AppContextMissing",
    "AppError",
    "AppRequestError",
    "AppTokenExpired",
    "MewboApp",
    "connect",
]
