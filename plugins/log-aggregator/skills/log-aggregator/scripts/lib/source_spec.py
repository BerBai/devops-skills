"""source_spec — parse <scheme>://<host>/<payload>?<query> source specs.

Public surface:
    ResolvedSource(scheme, host, payload, query, original_spec, source_tag)
    SpecError(ValueError)
    resolve(specs: list[str]) -> list[ResolvedSource]

Spec grammar (see plugins/log-aggregator/.../references/sources.md):
    journal://<host>/<unit?>
    docker://<host>/<container>
    kube://<host>/<ns>/<pod-or-glob>[?label=<sel>][?previous=true]
    file://<host>/<path>[?ts_regex=<r>&ts_format=<f>]

Brace expansion (single + nested):
    journal://web-{1,2,3}/nginx  -> 3 resolved sources
    {a,b}-{1,2}                  -> 4 expansions (Cartesian via recursion)

Glob in kube pod payload (`payment-svc-*`) is intentionally NOT expanded at
parse time — it requires a `kubectl get pods` round-trip and therefore lives
in fetchers._kube_fetcher.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ResolvedSource:
    """One concrete log source after brace expansion + scheme validation.

    Attributes:
        scheme: One of "journal" / "docker" / "kube" / "file".
        host: Either "local" (run on this machine) or an ssh-core alias.
        payload: Scheme-specific body (unit for journal, container for
            docker, "<ns>/<pod-or-glob>" for kube, "/path" for file).
        query: Parsed ``?k=v&k=v`` dict; empty if no query was supplied.
        original_spec: Verbatim user input — used for error messages so
            the user can identify which spec failed.
        source_tag: Short label used as the output prefix
            (e.g. ``web-1/nginx``).
    """

    scheme: str
    host: str
    payload: str
    query: dict[str, str]
    original_spec: str
    source_tag: str


class SpecError(ValueError):
    """Raised when a spec string cannot be parsed."""


_SUPPORTED_SCHEMES = frozenset({"journal", "docker", "kube", "file"})
_BRACE_RE = re.compile(r"\{([^{}]+)\}")
_SPEC_RE = re.compile(r"^(?P<scheme>[a-zA-Z]\w*)://(?P<rest>.+)$")


def resolve(specs: list[str]) -> list[ResolvedSource]:
    """Expand brace patterns then parse each into a ResolvedSource.

    Duplicate ``original_spec`` strings are collapsed (callers commonly pass
    ``web-{1,2,3}`` and ``web-1`` together when copy-pasting). Order of the
    first occurrence is preserved.
    """
    expanded: list[str] = []
    for spec in specs:
        expanded.extend(_expand_brace(spec))
    seen: set[str] = set()
    out: list[ResolvedSource] = []
    for s in expanded:
        if s in seen:
            continue
        seen.add(s)
        out.append(_parse_one(s))
    return out


def _expand_brace(spec: str) -> list[str]:
    """Recursively expand brace groups.

    Single layer:  ``web-{1,2,3}`` -> 3 strings.
    Nested:        ``{a,b}-{1,2}`` -> 4 strings (recursion handles the rest).
    No braces:     returns ``[spec]`` unchanged.
    Empty braces:  ``{}`` is treated as a single empty alternative (one
                   expansion with an empty substitution); this keeps the
                   parser well-defined without surprising users.
    """
    m = _BRACE_RE.search(spec)
    if not m:
        return [spec]
    options = m.group(1).split(",") if m.group(1) else [""]
    prefix = spec[: m.start()]
    suffix = spec[m.end() :]
    out: list[str] = []
    for opt in options:
        out.extend(_expand_brace(prefix + opt + suffix))
    return out


def _parse_one(spec: str) -> ResolvedSource:  # noqa: PLR0912 — 4 schemes × multiple validation branches
    m = _SPEC_RE.match(spec)
    if not m:
        raise SpecError(f"not a scheme://... spec: {spec!r}")
    scheme = m.group("scheme")
    rest = m.group("rest")

    if scheme not in _SUPPORTED_SCHEMES:
        raise SpecError(
            f"unsupported scheme {scheme!r} in {spec!r}; "
            f"expected one of {sorted(_SUPPORTED_SCHEMES)}"
        )

    # Split off the ?k=v[&k=v] query string before host/payload split so
    # that '?' inside the query does not confuse the host parser.
    query: dict[str, str] = {}
    if "?" in rest:
        rest, qstr = rest.split("?", 1)
        if qstr:
            for piece in qstr.split("&"):
                if not piece:
                    continue
                if "=" not in piece:
                    raise SpecError(
                        f"malformed query field {piece!r} in {spec!r}; expected k=v"
                    )
                k, v = piece.split("=", 1)
                query[k] = v

    parts = rest.split("/", 1)
    host = parts[0]
    payload = parts[1] if len(parts) > 1 else ""

    if not host:
        raise SpecError(f"missing host in {spec!r}")

    if scheme == "journal":
        source_tag = f"{host}/{payload}" if payload else f"{host}/system"
    elif scheme == "docker":
        if not payload:
            raise SpecError(f"docker:// requires <container>: {spec!r}")
        source_tag = f"{host}/{payload}"
    elif scheme == "kube":
        # payload = "<ns>/<pod-or-glob>"; pod may be omitted when ?label= is set
        if "/" not in payload and "label" not in query:
            raise SpecError(
                f"kube:// requires <ns>/<pod-or-glob> or ?label=<sel>: {spec!r}"
            )
        if "/" in payload:
            ns, pod = payload.split("/", 1)
            source_tag = f"{ns}-{pod}" if pod else ns
        else:
            source_tag = f"{payload}-{query.get('label', 'all')}"
    elif scheme == "file":
        if not payload:
            raise SpecError(f"file:// requires <path>: {spec!r}")
        if not payload.startswith("/"):
            payload = "/" + payload
        source_tag = f"{host}{payload}"
    else:  # pragma: no cover — guarded by _SUPPORTED_SCHEMES above
        raise SpecError(f"unhandled scheme {scheme!r}")

    return ResolvedSource(
        scheme=scheme,
        host=host,
        payload=payload,
        query=query,
        original_spec=spec,
        source_tag=source_tag,
    )
