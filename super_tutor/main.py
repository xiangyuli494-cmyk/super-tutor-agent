"""Super Tutor Agent — FastAPI application entry point."""

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from super_tutor import __version__


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    print(f"[super-tutor] v{__version__} starting up...")
    yield
    print("[super-tutor] shutting down.")


app = FastAPI(
    title="Super Tutor Agent",
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/v1/health")
async def health_check():
    return {"code": 0, "message": "ok", "data": {"version": __version__}}


# TODO: register routes — materials, quizzes, plans, dashboard
# app.include_router(materials.router, prefix="/api/v1")
# app.include_router(quizzes.router, prefix="/api/v1")
# app.include_router(plans.router, prefix="/api/v1")
# app.include_router(dashboard.router, prefix="/api/v1")


def main() -> None:
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="Super Tutor Agent server")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    uvicorn.run(
        "super_tutor.main:app",
        host="127.0.0.1",
        port=args.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
