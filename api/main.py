"""FastAPI control/read API. Placeholder until P5."""

from fastapi import FastAPI

app = FastAPI(title="Asymmetric Committee")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
