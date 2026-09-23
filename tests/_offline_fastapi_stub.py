"""
A minimal, stdlib-only stand-in for the tiny slice of FastAPI + Pydantic
that `app/main.py` actually uses (FastAPI, HTTPException, CORSMiddleware,
BaseModel, Depends/HTTPBasic for the admin auth dependency, and a
TestClient-like object).

WHY THIS EXISTS: the build sandbox this project was developed in has no
network access, so `pip install fastapi uvicorn pydantic` is not possible
here (verified — see PROGRESS.md). Rather than leave the web layer
completely unverified, this stub reimplements just enough request routing,
path-param extraction, and request/response (de)serialization to actually
*execute* every route handler in `app/main.py` and assert on real status
codes and response bodies.

This is a request-routing/logic verifier, not a substitute for FastAPI.
It does NOT validate what real FastAPI/Starlette/Pydantic would do around
things like: OpenAPI schema generation, header/query param handling,
Pydantic's full validation and coercion rules, async support, or WSGI/ASGI
correctness. `tests/test_api.py` prefers the real `fastapi.testclient` and
only falls back to this stub when `fastapi` isn't importable — so the exact
same test bodies become a strictly stronger check the moment you
`pip install -r requirements.txt` in an environment with network access.
"""
from __future__ import annotations

import json as _json
import re
import sys
import types
from typing import Any, Optional, get_args, get_origin


# ---------------------------------------------------------------------------
# pydantic.BaseModel (minimal subset: annotated fields, class-level defaults,
# required fields, model_dump(exclude=..., exclude_none=...))
# ---------------------------------------------------------------------------

_MISSING = object()


class BaseModel:
    def __init__(self, **data: Any) -> None:
        fields = self._collect_fields()
        for name, default in fields.items():
            if name in data:
                value = data.pop(name)
            elif default is not _MISSING:
                value = default
            else:
                raise TypeError(f"{type(self).__name__} missing required field: {name!r}")
            setattr(self, name, value)
        if data:
            # Real pydantic (v2, non-strict) ignores unknown fields by
            # default too, so we mirror that rather than raising.
            pass

    @classmethod
    def _collect_fields(cls) -> dict:
        fields: dict[str, Any] = {}
        for klass in reversed(cls.__mro__):
            annotations = klass.__dict__.get("__annotations__", {})
            for name in annotations:
                if name.startswith("_"):
                    continue
                default = klass.__dict__.get(name, _MISSING)
                fields[name] = default
        return fields

    def model_dump(self, exclude: Optional[set] = None, exclude_none: bool = False) -> dict:
        exclude = exclude or set()
        out = {}
        for name in self._collect_fields():
            if name in exclude:
                continue
            value = getattr(self, name)
            if exclude_none and value is None:
                continue
            out[name] = value
        return out

    def __repr__(self) -> str:
        fields = ", ".join(f"{k}={v!r}" for k, v in self.model_dump().items())
        return f"{type(self).__name__}({fields})"


# ---------------------------------------------------------------------------
# fastapi.HTTPException / FastAPI
# ---------------------------------------------------------------------------

class HTTPException(Exception):
    def __init__(self, status_code: int, detail: Any = None, headers: Optional[dict] = None):
        self.status_code = status_code
        self.detail = detail
        self.headers = headers
        super().__init__(detail)


class _QueryMarker:
    """Stands in for fastapi.Query(default, ge=..., le=...). The stub does
    NOT enforce ge/le/etc (documented gap, same idiom as the class
    docstring's existing list of things this stub doesn't validate) — it
    just carries the default through and lets StubTestClient pull the real
    value out of the request's query string when present."""

    def __init__(self, default: Any = None, **constraints: Any):
        self.default = default
        self.constraints = constraints


def Query(default: Any = None, **constraints: Any) -> _QueryMarker:
    return _QueryMarker(default, **constraints)


class Request:
    """Minimal stand-in so `Request` is importable for type hints. The stub
    client does not run `@app.middleware("http")` functions at all (see
    FastAPI.middleware below) so this is never actually instantiated —
    it only needs to exist so `from fastapi import Request` succeeds and
    `typing.get_type_hints` can resolve the annotation string."""


class _DependsMarker:
    """Stands in for fastapi.Depends(...). Real FastAPI resolves these by
    inspecting the callable's own parameters recursively; _call_dependency
    below does the same, just enough for this app's one dependency chain
    (verify_admin -> HTTPBasic)."""

    def __init__(self, dependency):
        self.dependency = dependency


def Depends(dependency):
    return _DependsMarker(dependency)


class HTTPBasicCredentials(BaseModel):
    username: str
    password: str


class HTTPBasic:
    """Stub for fastapi.security.HTTPBasic. Real FastAPI parses the
    Authorization header for you; here that parsing happens explicitly in
    __call__, invoked by _call_dependency with whatever headers dict the
    stub client was given."""

    def __init__(self, **kwargs):
        pass

    def __call__(self, headers: dict) -> HTTPBasicCredentials:
        import base64

        auth = (headers or {}).get("Authorization") or (headers or {}).get("authorization")
        if not auth or not auth.lower().startswith("basic "):
            raise HTTPException(
                status_code=401,
                detail="Not authenticated",
                headers={"WWW-Authenticate": "Basic"},
            )
        try:
            decoded = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
            username, password = decoded.split(":", 1)
        except Exception:
            raise HTTPException(
                status_code=401,
                detail="Invalid basic auth header",
                headers={"WWW-Authenticate": "Basic"},
            )
        return HTTPBasicCredentials(username=username, password=password)


def _call_dependency(func, headers: dict):
    """Resolve and call a dependency function, recursively resolving any of
    its own parameters that are themselves Depends(...) markers. Only
    handles the shapes this app actually uses (HTTPBasic leaf, plain
    functions above it) — not a general DI container."""
    import inspect

    sig = inspect.signature(func)
    kwargs = {}
    for name, param in sig.parameters.items():
        default = param.default
        if isinstance(default, _DependsMarker):
            dep = default.dependency
            if isinstance(dep, HTTPBasic):
                kwargs[name] = dep(headers)
            else:
                kwargs[name] = _call_dependency(dep, headers)
    return func(**kwargs)


class _Route:
    def __init__(self, method: str, path: str, func, dependencies: Optional[list] = None):
        self.method = method
        self.path = path
        self.func = func
        self.dependencies = dependencies or []
        param_names = re.findall(r"\{(\w+)\}", path)
        pattern = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", path)
        self.regex = re.compile(f"^{pattern}$")
        self.param_names = param_names

    def match(self, method: str, path: str):
        if method != self.method:
            return None
        m = self.regex.match(path)
        if not m:
            return None
        return m.groupdict()


class FastAPI:
    def __init__(self, title: str = "", version: str = ""):
        self.title = title
        self.version = version
        self.routes: list[_Route] = []
        self.middlewares: list[tuple] = []

    def add_middleware(self, middleware_class, **options):
        self.middlewares.append((middleware_class, options))

    def middleware(self, middleware_type: str):
        """Stub for @app.middleware("http"). Deliberately a no-op that just
        returns the function unchanged — this stub has no real ASGI
        request/response pipeline to hang middleware off (see the module
        docstring's list of things it doesn't reimplement). Request
        logging and rate limiting are therefore untested in stub mode;
        they're covered by tests/test_rate_limiter.py directly (stdlib
        only, no fastapi needed) and become exercised end-to-end the
        moment real fastapi/uvicorn are installed."""
        def decorator(func):
            return func
        return decorator

    def _route_decorator(self, method: str, path: str, **kw):
        def decorator(func):
            self.routes.append(_Route(method, path, func, dependencies=kw.get("dependencies")))
            return func
        return decorator

    def get(self, path: str, **kw):
        return self._route_decorator("GET", path, **kw)

    def post(self, path: str, **kw):
        return self._route_decorator("POST", path, **kw)

    def patch(self, path: str, **kw):
        return self._route_decorator("PATCH", path, **kw)

    def resolve(self, method: str, path: str):
        # Exact/static routes should win over templated ones if both match
        # (not actually ambiguous for this app's route table, but keep the
        # matching order-independent and deterministic).
        candidates = []
        for route in self.routes:
            params = route.match(method, path)
            if params is not None:
                candidates.append((route, params))
        if not candidates:
            return None, None
        candidates.sort(key=lambda rp: len(rp[0].param_names))
        return candidates[0]


# fastapi.middleware.cors.CORSMiddleware — never actually applied by the
# stub client (no real ASGI stack to hang it off), just accepted so
# `app.add_middleware(CORSMiddleware, ...)` in main.py doesn't error.
class CORSMiddleware:
    def __init__(self, *args, **kwargs):
        pass


# ---------------------------------------------------------------------------
# A TestClient-alike. Mirrors the handful of methods/attributes
# tests/test_api.py relies on: client.get/post(path, json=...) -> response
# with .status_code and .json().
# ---------------------------------------------------------------------------

class _StubResponse:
    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


def _serialize(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, list):
        return [_serialize(v) for v in value]
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    return value


def _is_basemodel_annotation(annotation) -> bool:
    try:
        return isinstance(annotation, type) and issubclass(annotation, BaseModel)
    except TypeError:
        return False


class StubTestClient:
    """Drop-in-enough replacement for fastapi.testclient.TestClient."""

    def __init__(self, app: FastAPI):
        self.app = app

    def _request(self, method: str, path: str, json: Optional[dict] = None, headers: Optional[dict] = None) -> _StubResponse:
        import inspect
        import typing
        from urllib.parse import parse_qsl, urlsplit

        split = urlsplit(path)
        path_only = split.path
        query_params = dict(parse_qsl(split.query))

        route, path_params = self.app.resolve(method, path_only)
        if route is None:
            return _StubResponse(404, {"detail": "Not Found"})

        try:
            # Route-level dependencies (e.g. dependencies=[Depends(verify_admin)])
            # run before the handler itself, same order as real FastAPI.
            for dep_marker in route.dependencies:
                _call_dependency(dep_marker.dependency, headers or {})
        except HTTPException as exc:
            return _StubResponse(exc.status_code, {"detail": exc.detail})

        sig = inspect.signature(route.func)
        # main.py uses `from __future__ import annotations`, so
        # param.annotation is a *string* ("MessageRequest") rather than the
        # class object — resolve real annotations via get_type_hints, which
        # evaluates those strings against the function's own module globals
        # (where FastAPI/pydantic names were imported, real or stubbed).
        try:
            hints = typing.get_type_hints(route.func)
        except Exception:
            hints = {}

        call_kwargs = {}
        for name, param in sig.parameters.items():
            annotation = hints.get(name, param.annotation)
            if name in path_params:
                call_kwargs[name] = path_params[name]
            elif isinstance(param.default, _QueryMarker):
                raw = query_params.get(name)
                if raw is None:
                    call_kwargs[name] = param.default.default
                elif annotation in (int, float):
                    call_kwargs[name] = annotation(raw)
                else:
                    call_kwargs[name] = raw
            elif isinstance(param.default, _DependsMarker):
                try:
                    call_kwargs[name] = _call_dependency(param.default.dependency, headers or {})
                except HTTPException as exc:
                    return _StubResponse(exc.status_code, {"detail": exc.detail})
            elif _is_basemodel_annotation(annotation):
                body = json or {}
                call_kwargs[name] = annotation(**body)
            elif param.default is not inspect.Parameter.empty:
                continue
            else:
                # Shouldn't happen for this app's handlers, but fail loudly
                # rather than silently miscalling the route.
                raise TypeError(
                    f"Stub client can't supply parameter {name!r} for {route.path}"
                )

        try:
            result = route.func(**call_kwargs)
        except HTTPException as exc:
            return _StubResponse(exc.status_code, {"detail": exc.detail})

        return _StubResponse(200, _serialize(result))

    def get(self, path: str, headers: Optional[dict] = None, **kw) -> _StubResponse:
        return self._request("GET", path, headers=headers)

    def post(self, path: str, json: Optional[dict] = None, headers: Optional[dict] = None, **kw) -> _StubResponse:
        return self._request("POST", path, json=json, headers=headers)

    def patch(self, path: str, json: Optional[dict] = None, headers: Optional[dict] = None, **kw) -> _StubResponse:
        return self._request("PATCH", path, json=json, headers=headers)


def install_stub_fastapi() -> None:
    """Register fake `fastapi` / `fastapi.middleware.cors` / `pydantic`
    modules in sys.modules so `app/main.py` (which does
    `from fastapi import FastAPI, HTTPException` etc.) imports cleanly.

    Only call this after confirming the real packages aren't installed —
    see the import-fallback logic in tests/test_api.py.
    """
    fastapi_mod = types.ModuleType("fastapi")
    fastapi_mod.FastAPI = FastAPI
    fastapi_mod.HTTPException = HTTPException
    fastapi_mod.Depends = Depends
    fastapi_mod.Query = Query
    fastapi_mod.Request = Request

    middleware_mod = types.ModuleType("fastapi.middleware")
    cors_mod = types.ModuleType("fastapi.middleware.cors")
    cors_mod.CORSMiddleware = CORSMiddleware
    middleware_mod.cors = cors_mod
    fastapi_mod.middleware = middleware_mod

    security_mod = types.ModuleType("fastapi.security")
    security_mod.HTTPBasic = HTTPBasic
    security_mod.HTTPBasicCredentials = HTTPBasicCredentials
    fastapi_mod.security = security_mod

    responses_mod = types.ModuleType("fastapi.responses")

    class JSONResponse:
        """Stub for fastapi.responses.JSONResponse — only ever constructed
        inside rate_limit_middleware, which the stub never calls (see
        FastAPI.middleware), so this just needs to exist as an import
        target."""

        def __init__(self, status_code: int = 200, content: Any = None, headers: Optional[dict] = None):
            self.status_code = status_code
            self.content = content
            self.headers = headers or {}

    responses_mod.JSONResponse = JSONResponse
    fastapi_mod.responses = responses_mod

    pydantic_mod = types.ModuleType("pydantic")
    pydantic_mod.BaseModel = BaseModel

    sys.modules["fastapi"] = fastapi_mod
    sys.modules["fastapi.middleware"] = middleware_mod
    sys.modules["fastapi.middleware.cors"] = cors_mod
    sys.modules["fastapi.security"] = security_mod
    sys.modules["fastapi.responses"] = responses_mod
    sys.modules["pydantic"] = pydantic_mod
