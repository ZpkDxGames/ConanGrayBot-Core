from backend.api import app


if __name__ == "__main__":
    import uvicorn

    from backend.config import get_settings

    settings = get_settings()
    uvicorn.run("main:app", host=settings.host, port=settings.port, reload=False)

