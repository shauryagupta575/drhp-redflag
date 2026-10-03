from fastapi import FastAPI

app = FastAPI(title="DRHP Red-Flag Agent API", version="0.1.0")


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    """Liveness check."""
    return {"status": "ok"}
