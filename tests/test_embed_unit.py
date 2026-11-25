"""Deterministic embedding tests: no model, accelerator or network required."""
from __future__ import annotations

import importlib
import importlib.util
import io
import json
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pytest


@pytest.fixture
def backend(monkeypatch):
    module = importlib.import_module("recall.embed")
    monkeypatch.setattr(module, "EMBED_BASE_URL", "http://embedding.invalid:3210")
    monkeypatch.setattr(module, "EMBED_URL", "http://embedding.invalid:3210/v1/embeddings")
    monkeypatch.setattr(module, "EMBED_MODEL", "unit-embed")
    monkeypatch.setattr(module, "_EMBEDDING_CACHE", {})
    return module


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"data": [{"id": "unit-embed"}]}, True),
        ({"data": [{"id": "unit-embed:latest"}]}, True),
        ({"data": [{"id": "unit-embed:q4"}]}, True),
        ({"data": [{"id": "unit-embed-other"}]}, False),
        ({"data": [{"id": "other-model"}]}, False),
        ({"data": []}, False),
        ({}, False),
        ({"data": [None]}, False),
        ({"data": [{"id": None}]}, False),
        ({"data": None}, False),
    ],
)
def test_model_probe_checks_the_configured_endpoint_and_model(backend, monkeypatch, payload, expected):
    request = Mock(return_value=io.BytesIO(json.dumps(payload).encode()))
    monkeypatch.setattr(backend.urllib.request, "urlopen", request)
    assert backend.is_loaded() is expected
    req = request.call_args.args[0]
    assert req.full_url == "http://embedding.invalid:3210/v1/models"
    assert request.call_args.kwargs == {"timeout": 2}


@pytest.mark.parametrize(
    "error",
    [
        URLError("connection refused"),
        TimeoutError("backend timed out"),
        HTTPError("http://embedding.invalid/v1/models", 503, "unavailable", {}, None),
    ],
)
def test_model_probe_degrades_when_backend_is_unavailable(backend, monkeypatch, error):
    monkeypatch.setattr(backend.urllib.request, "urlopen", Mock(side_effect=error))
    assert backend.is_loaded() is False


def test_model_probe_rejects_invalid_json(backend, monkeypatch):
    monkeypatch.setattr(backend.urllib.request, "urlopen", Mock(return_value=io.BytesIO(b"not JSON")))
    assert backend.is_loaded() is False


def test_embed_caches_success_and_uses_the_configured_request(backend, monkeypatch):
    vector = [0.25] * 768
    request = Mock(return_value=io.BytesIO(json.dumps({"data": [{"embedding": vector}]}).encode()))
    monkeypatch.setattr(backend.urllib.request, "urlopen", request)
    assert backend.embed("unit query") == vector
    assert backend.embed("unit query") == vector
    request.assert_called_once()
    req = request.call_args.args[0]
    assert req.full_url == backend.EMBED_URL
    assert json.loads(req.data) == {
        "model": "unit-embed", "input": ["unit query"], "encoding_format": "float",
    }
    assert request.call_args.kwargs == {"timeout": backend.EMBED_TIMEOUT}


def test_embed_does_not_cache_an_outage_and_can_recover(backend, monkeypatch):
    vector = [0.5] * 768
    request = Mock(side_effect=[
        URLError("offline"),
        io.BytesIO(json.dumps({"data": [{"embedding": vector}]}).encode()),
    ])
    monkeypatch.setattr(backend.urllib.request, "urlopen", request)
    assert backend.embed("retry after outage") is None
    assert backend._EMBEDDING_CACHE == {}
    assert backend.embed("retry after outage") == vector
    assert request.call_count == 2


def _load_live_test(monkeypatch, opt_in, available):
    if opt_in is None:
        monkeypatch.delenv("RECALL_TEST_EMBEDDINGS", raising=False)
    else:
        monkeypatch.setenv("RECALL_TEST_EMBEDDINGS", opt_in)
    module = importlib.import_module("recall.embed")
    probe = Mock(return_value=available)
    monkeypatch.setattr(module, "is_loaded", probe)
    spec = importlib.util.spec_from_file_location(
        "recall_live_embedding_gate_probe", Path(__file__).with_name("test_core.py"),
    )
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    marker = next(mark for mark in loaded.test_embed.pytestmark if mark.name == "skipif")
    return loaded.test_embed, marker, probe


@pytest.mark.parametrize("opt_in", [None, "0", "true"])
def test_default_suite_never_probes_a_live_embedding_backend(monkeypatch, opt_in):
    _test, marker, probe = _load_live_test(monkeypatch, opt_in, available=True)
    assert marker.args == (True,)
    probe.assert_not_called()


@pytest.mark.parametrize("available", [False, True])
def test_explicit_live_opt_in_still_checks_backend_availability(monkeypatch, available):
    _test, marker, probe = _load_live_test(monkeypatch, "1", available)
    assert marker.args == (not available,)
    probe.assert_called_once_with()


def test_live_embedding_failure_is_not_downgraded_to_a_skip(monkeypatch):
    test, marker, _probe = _load_live_test(monkeypatch, "1", available=True)
    monkeypatch.setitem(test.__globals__, "embed", Mock(return_value=None))
    assert marker.args == (False,)
    with pytest.raises(AssertionError):
        test()
