"""T-27: dashboard shell. Every nav item renders the shell with a "coming soon" placeholder."""

from __future__ import annotations

import base64
import hashlib
import re

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from hive.api.app import NAV, PLACEHOLDERS, create_app
from hive.store.sqlite import SqliteStore


@pytest.fixture
def client():
    store = SqliteStore(":memory:")
    yield TestClient(create_app(store=store))
    store.close()


def test_nav_is_the_five_sections_in_order():
    assert [label for _, label, _ in NAV] == ["Radar", "Decisions", "Timeline", "Ask", "Impact"]
    assert set(PLACEHOLDERS) <= {key for key, _, _ in NAV}


def test_root_renders_the_shell_with_the_full_nav(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    links = re.findall(r'<a href="(/[a-z]*)"\s*(aria-current="page")?>(\w+)</a>', r.text)
    assert [(path, label) for path, _, label in links] == [(path, label) for _, label, path in NAV]
    assert [label for _, current, label in links if current] == ["Radar"]  # opens on the first section


@pytest.mark.parametrize(("key", "label", "path"), NAV)
def test_each_nav_item_shows_coming_soon_and_is_marked_active(client, key, label, path):
    r = client.get(path)
    assert r.status_code == 200
    assert f"<h1>{label}</h1>" in r.text
    assert "Coming soon" in r.text
    assert str(escape(PLACEHOLDERS[key])) in r.text
    assert re.findall(r'<a href="([^"]+)"\s+aria-current="page"', r.text) == [path]


# sha384 of the upstream files on unpkg (htmx.org@2.0.4/dist/htmx.min.js, htmx-ext-sse@2.2.2/sse.js).
VENDOR = "/static/vendor/"
VENDORED = {
    VENDOR + "htmx-2.0.4.min.js": "HGfztofotfshcF7+8n44JQL2oJmowVChPTg48S+jvZoztPfvwD79OC/LTtG6dMp+",
    VENDOR + "htmx-ext-sse-2.2.2.js": "fw+eTlCc7suMV/1w/7fr2/PmwElUIt5i82bi+qTiLXvjRXZ2/FkiTNA/w0MhXnGI",
}


def test_scripts_are_vendored_unmodified_and_nothing_loads_from_the_network(client):
    page = client.get("/").text
    assert re.findall(r'<script src="([^"]+)"', page) == [*VENDORED, "/static/app.js"]
    assert not re.findall(r'(?:src|href)="https?://', page)
    for path, sha384 in VENDORED.items():
        body = client.get(path).content
        assert base64.b64encode(hashlib.sha384(body).digest()).decode() == sha384
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_shell_never_exposes_harnessrouter_to_the_browser(client):
    served = client.get("/").text + client.get("/static/app.js").text
    assert "sk-hr-" not in served
    assert "harnessrouter.ai" not in served


def test_dashboard_pages_stay_out_of_the_api_schema(client):
    assert all(path.startswith("/api/") for path in client.get("/openapi.json").json()["paths"])


def test_unknown_page_is_404(client):
    assert client.get("/nope").status_code == 404
