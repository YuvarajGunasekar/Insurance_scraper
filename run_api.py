"""Convenience entry point for running the local scraping API."""

import uvicorn


if __name__ == "__main__":
    uvicorn.run("api.main:app", host="127.0.0.1", port=8000, reload=False)