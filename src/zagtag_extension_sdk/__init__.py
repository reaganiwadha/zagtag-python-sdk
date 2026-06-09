"""Python SDK for Zagtag extensions.

Mirrors the Rust SDK's loopback HTTP protocol so Python and Rust extensions are
wire-identical. Standard library only — no FastAPI/pydantic — so every bundled
extension boots without a heavy dependency tree.

Usage:

    from zagtag_extension_sdk import (
        ExtensionManifest, DerivatorContribution, PreviewContribution,
        DerivatorContext, DerivatorOutput, run_extension,
    )

    def thumbnail(ctx: DerivatorContext) -> DerivatorOutput:
        data = open(ctx.local_path(), "rb").read()
        return DerivatorOutput("application/json", "json", b"{}")

    run_extension(ExtensionManifest("zagtag.example", "Example", "0.1.0")) \\
        .with_derivator(DerivatorContribution(...), thumbnail) \\
        .serve()
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import tempfile
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional


# --- Manifest models (mirror zagtag-extension-sdk/src/manifest.rs) ---


@dataclass
class PreviewContribution:
    kind: str
    description: str = ""
    weight: int = 1

    def to_json(self) -> dict:
        return {"kind": self.kind, "description": self.description, "weight": self.weight}


@dataclass
class DerivatorContribution:
    derivator_id: str
    display_name: str
    description: str
    applicability: dict
    output_mime_type: str
    output_extension: str
    provides_preview: Optional[PreviewContribution] = None

    def to_json(self) -> dict:
        out = {
            "derivator_id": self.derivator_id,
            "display_name": self.display_name,
            "description": self.description,
            "applicability": self.applicability,
            "output_mime_type": self.output_mime_type,
            "output_extension": self.output_extension,
        }
        if self.provides_preview is not None:
            out["provides_preview"] = self.provides_preview.to_json()
        return out


@dataclass
class TagMetaDefinition:
    """Optional, advisory declaration of a tag-metadata key. The host does not
    validate emitted meta against this; it is a hint for typing/UI only."""

    name: str
    value_type: str  # "bool" | "int" | "float" | "string" — hint only; meta is text.
    unit: Optional[str] = None

    def to_json(self) -> dict:
        out = {"name": self.name, "value_type": self.value_type}
        if self.unit is not None:
            out["unit"] = self.unit
        return out


@dataclass
class TagDefinition:
    key: str
    value_type: str
    unit: Optional[str] = None
    meta: list = field(default_factory=list)  # list[TagMetaDefinition], advisory

    def to_json(self) -> dict:
        out = {"key": self.key, "value_type": self.value_type}
        if self.unit is not None:
            out["unit"] = self.unit
        if self.meta:
            out["meta"] = [m.to_json() for m in self.meta]
        return out


@dataclass
class TaggerContribution:
    tagger_id: str
    display_name: str
    description: str
    applicability: dict
    emits_tags: list[TagDefinition] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "tagger_id": self.tagger_id,
            "display_name": self.display_name,
            "description": self.description,
            "applicability": self.applicability,
            "emits_tags": [t.to_json() for t in self.emits_tags],
        }


@dataclass
class ExtensionManifest:
    extension_id: str
    display_name: str
    version: str

    def to_json(self) -> dict:
        return {
            "extension_id": self.extension_id,
            "display_name": self.display_name,
            "version": self.version,
        }


# --- Derivator runtime types ---


@dataclass
class DerivatorOutput:
    mime_type: str
    extension: str
    data: bytes


def _local_path_for(object_desc: dict, object_source: dict) -> str:
    src = object_source or {}
    kind = src.get("kind")
    if kind == "local_path":
        return src["path"]
    if kind == "fetch_url":
        suffix = "-" + object_desc.get("relative_path", "obj").split("/")[-1]
        fd, tmp = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "wb") as out:
            with urllib.request.urlopen(src["url"]) as resp:  # noqa: S310
                out.write(resp.read())
        return tmp
    raise ValueError(f"unsupported object_source kind: {kind!r}")


@dataclass
class DerivatorContext:
    object: dict
    object_source: dict
    config: dict = field(default_factory=dict)

    def local_path(self) -> str:
        """Resolve the object's bytes to a local file path.

        `local_path` sources return the path directly; `fetch_url` sources are
        streamed to a temp file so derivator code never changes per-transport.
        """
        return _local_path_for(self.object, self.object_source)


DerivatorFn = Callable[[DerivatorContext], DerivatorOutput]


# --- Tagger runtime types ---


# Reserved metadata key for a tagger's confidence in a tag (a float in [0, 1] by
# convention, formatted to a string). The search backend filters/sorts on it.
CONFIDENCE_META = "confidence"


@dataclass
class EmittedTag:
    key: str
    value_type: str
    string_value: Optional[str] = None
    int_value: Optional[int] = None
    float_value: Optional[float] = None
    bool_value: Optional[bool] = None
    # Free-form metadata as (name, value) pairs; value is always a string.
    meta: list = field(default_factory=list)

    def with_meta(self, name: str, value: str) -> "EmittedTag":
        """Attach a free-form metadata attribute to this tag. Chainable."""
        self.meta.append((name, value))
        return self

    def with_confidence(self, confidence: float) -> "EmittedTag":
        """Attach the reserved ``confidence`` attribute. Chainable."""
        return self.with_meta(CONFIDENCE_META, str(confidence))

    @staticmethod
    def string(key: str, value: str) -> "EmittedTag":
        return EmittedTag(key=key, value_type="string", string_value=value)

    @staticmethod
    def color(key: str, value: str) -> "EmittedTag":
        return EmittedTag(key=key, value_type="color", string_value=value)

    @staticmethod
    def int(key: str, value: int) -> "EmittedTag":
        return EmittedTag(key=key, value_type="int", int_value=value)

    @staticmethod
    def float(key: str, value: float) -> "EmittedTag":
        return EmittedTag(key=key, value_type="float", float_value=value)

    @staticmethod
    def bool(key: str, value: bool) -> "EmittedTag":
        return EmittedTag(key=key, value_type="bool", bool_value=value)

    def to_json(self) -> dict:
        out = {
            "key": self.key,
            "value_type": self.value_type,
            "string_value": self.string_value,
            "int_value": self.int_value,
            "float_value": self.float_value,
            "bool_value": self.bool_value,
        }
        if self.meta:
            out["meta"] = [{"name": n, "value": v} for (n, v) in self.meta]
        return out


@dataclass
class EmittedIndexText:
    key: str
    text: str

    def to_json(self) -> dict:
        return {"key": self.key, "text": self.text}


@dataclass
class TaggerOutput:
    tags: list[EmittedTag] = field(default_factory=list)
    index_texts: list[EmittedIndexText] = field(default_factory=list)
    outcome: str = "tagged"

    @staticmethod
    def not_applicable() -> "TaggerOutput":
        return TaggerOutput(outcome="not_applicable")

    @staticmethod
    def with_tags(tags: list[EmittedTag]) -> "TaggerOutput":
        return TaggerOutput(tags=tags)

    @staticmethod
    def with_index_texts(index_texts: list[EmittedIndexText]) -> "TaggerOutput":
        return TaggerOutput(index_texts=index_texts)

    def to_json(self) -> dict:
        return {
            "outcome": self.outcome,
            "tags": [t.to_json() for t in self.tags],
            "index_texts": [t.to_json() for t in self.index_texts],
        }


@dataclass
class TaggerContext:
    object: dict
    object_source: dict
    config: dict = field(default_factory=dict)

    def local_path(self) -> str:
        return _local_path_for(self.object, self.object_source)


TaggerFn = Callable[[TaggerContext], TaggerOutput]


# --- Server ---


class _ExtensionApp:
    def __init__(self, manifest: ExtensionManifest):
        self.manifest = manifest
        self.contributions: list[DerivatorContribution] = []
        self.tagger_contributions: list[TaggerContribution] = []
        self.derivators: dict[str, DerivatorFn] = {}
        self.taggers: dict[str, TaggerFn] = {}
        # Boot config delivered via --config; merged as the base of every
        # context's config (per-call request config wins).
        self.boot_config: dict = {}

    def _merged_config(self, request_config: dict) -> dict:
        if not self.boot_config:
            return request_config or {}
        return {**self.boot_config, **(request_config or {})}

    def with_derivator(
        self, contribution: DerivatorContribution, fn: DerivatorFn
    ) -> "_ExtensionApp":
        self.contributions.append(contribution)
        self.derivators[contribution.derivator_id] = fn
        return self

    def with_tagger(
        self, contribution: TaggerContribution, fn: TaggerFn
    ) -> "_ExtensionApp":
        self.tagger_contributions.append(contribution)
        self.taggers[contribution.tagger_id] = fn
        return self

    def describe(self) -> dict:
        return {
            "manifest": self.manifest.to_json(),
            "derivators": [c.to_json() for c in self.contributions],
            "taggers": [c.to_json() for c in self.tagger_contributions],
        }

    def run_derivator(self, body: dict) -> tuple[int, dict]:
        derivator_id = body.get("derivator_id")
        fn = self.derivators.get(derivator_id)
        if fn is None:
            return 404, {"ok": False, "error": f"unknown derivator_id: {derivator_id}"}
        ctx = DerivatorContext(
            object=body.get("object", {}),
            object_source=body.get("object_source", {}),
            config=self._merged_config(body.get("config", {})),
        )
        try:
            output = fn(ctx)
        except Exception as exc:  # noqa: BLE001 — surface as a structured error
            return 500, {"ok": False, "error": str(exc)}
        return 200, {
            "ok": True,
            "result": {
                "mime_type": output.mime_type,
                "extension": output.extension,
                "bytes_b64": base64.b64encode(output.data).decode("ascii"),
            },
        }

    def run_tagger(self, body: dict) -> tuple[int, dict]:
        tagger_id = body.get("tagger_id")
        fn = self.taggers.get(tagger_id)
        if fn is None:
            return 404, {"ok": False, "error": f"unknown tagger_id: {tagger_id}"}
        ctx = TaggerContext(
            object=body.get("object", {}),
            object_source=body.get("object_source", {}),
            config=self._merged_config(body.get("config", {})),
        )
        try:
            output = fn(ctx)
        except Exception as exc:  # noqa: BLE001 - surface as a structured error
            return 500, {"ok": False, "error": str(exc)}
        return 200, {"ok": True, "result": output.to_json()}

    def serve(self, port: int = 0) -> None:
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):  # quiet; host drains stdout
                pass

            def handle_one_request(self):  # noqa: N802
                try:
                    super().handle_one_request()
                except CLIENT_DISCONNECT_ERRORS:
                    return

            def _send(self, status: int, payload: dict):
                body = json.dumps(payload).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except CLIENT_DISCONNECT_ERRORS:
                    return

            def do_GET(self):  # noqa: N802
                if self.path == "/health":
                    self._send(200, {"ok": True})
                elif self.path == "/describe":
                    self._send(200, app.describe())
                else:
                    self._send(404, {"ok": False, "error": "not found"})

            def do_POST(self):  # noqa: N802
                if self.path not in {"/derivators/run", "/taggers/run"}:
                    self._send(404, {"ok": False, "error": "not found"})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw or b"{}")
                except json.JSONDecodeError as exc:
                    self._send(400, {"ok": False, "error": f"bad json: {exc}"})
                    return
                if self.path == "/derivators/run":
                    status, payload = app.run_derivator(body)
                else:
                    status, payload = app.run_tagger(body)
                self._send(status, payload)

        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        bound_port = server.server_address[1]
        # The host reads this line on stdout to learn the port and that we are up.
        print(f"READY {bound_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--extension-id", type=str, default=None)
    parser.add_argument("--config", type=str, default=None)
    args, _unknown = parser.parse_known_args()
    return args


def _parse_boot_config(raw: Optional[str]) -> dict:
    """Parse the --config JSON object. Empty/None/non-object → {}."""
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


class ExtensionBuilder:
    def __init__(self, manifest: ExtensionManifest):
        self._app = _ExtensionApp(manifest)

    def with_derivator(
        self, contribution: DerivatorContribution, fn: DerivatorFn
    ) -> "ExtensionBuilder":
        self._app.with_derivator(contribution, fn)
        return self

    def with_tagger(
        self, contribution: TaggerContribution, fn: TaggerFn
    ) -> "ExtensionBuilder":
        self._app.with_tagger(contribution, fn)
        return self

    def serve(self) -> None:
        args = _parse_args()
        self._app.boot_config = _parse_boot_config(args.config)
        self._app.serve(port=args.port)


def run_extension(manifest: ExtensionManifest) -> ExtensionBuilder:
    return ExtensionBuilder(manifest)


__all__ = [
    "PreviewContribution",
    "DerivatorContribution",
    "TagDefinition",
    "TagMetaDefinition",
    "CONFIDENCE_META",
    "TaggerContribution",
    "ExtensionManifest",
    "DerivatorOutput",
    "DerivatorContext",
    "EmittedTag",
    "EmittedIndexText",
    "TaggerOutput",
    "TaggerContext",
    "ExtensionBuilder",
    "run_extension",
]
CLIENT_DISCONNECT_ERRORS = (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)
