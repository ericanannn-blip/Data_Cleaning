"""Exercise the app under the URL prefix used by an embedded Web Preview."""

from contextlib import asynccontextmanager
import os

from fastapi import FastAPI
import uvicorn

from backend.main import create_app


application = create_app()


@asynccontextmanager
async def lifespan(app):
    async with application.router.lifespan_context(application):
        yield


app = FastAPI(lifespan=lifespan)
app.mount("/preview", application)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ["PORT"]))
