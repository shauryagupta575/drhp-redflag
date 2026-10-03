from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_returns_ok() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_rejects_post() -> None:
    assert client.post("/health").status_code == 405


def test_openapi_lists_health() -> None:
    assert "/health" in client.get("/openapi.json").json()["paths"]
