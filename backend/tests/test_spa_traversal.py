"""The SPA fallback must never serve a file from outside the frontend directory."""

import os

import pytest
from fastapi.testclient import TestClient

from app.main import FRONTEND_DIR, app

SECRET = "TOP-SECRET-OUTSIDE-THE-FRONTEND-DIR"


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def outside_file():
    """A file next to (not inside) the frontend directory."""
    path = os.path.join(os.path.dirname(FRONTEND_DIR), "outside-secret.txt")
    with open(path, "w") as f:
        f.write(SECRET)
    yield path
    os.remove(path)


@pytest.mark.parametrize(
    "template",
    [
        "/..%2foutside-secret.txt",
        "/%2e%2e/outside-secret.txt",
        "/%2e%2e%2foutside-secret.txt",
        "/..%5coutside-secret.txt",
        "/....//outside-secret.txt",
        "//{abs_path}",
        "/{abs_path}",
    ],
)
def test_paths_outside_the_frontend_dir_are_not_served(client, outside_file, template):
    url = template.format(abs_path=outside_file.replace("\\", "/").lstrip("/"))
    response = client.get(url)
    assert SECRET not in response.text
    # An unknown path falls back to the app shell, never to a file on disk.
    assert response.status_code in (200, 404)


def test_a_file_inside_the_frontend_dir_is_still_served(client):
    path = os.path.join(FRONTEND_DIR, "manifest.webmanifest")
    with open(path, "w") as f:
        f.write('{"name": "VibeHealth"}')
    try:
        response = client.get("/manifest.webmanifest")
        assert response.status_code == 200
        assert "VibeHealth" in response.text
    finally:
        os.remove(path)


def test_unknown_page_returns_the_app_shell(client):
    response = client.get("/documents/1")
    assert response.status_code == 200
    assert "<title>test</title>" in response.text


@pytest.mark.parametrize("path", ["/%00", "/assets%00.js", "/a%00/b", "/assets/%00", "/assets/a%00.js"])
def test_a_nul_byte_in_the_path_is_a_404_not_a_500(client, path):
    assert client.get(path).status_code == 404
