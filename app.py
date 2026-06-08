from __future__ import annotations

import asyncio
import json
import queue as stdlib_queue
import sys
import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from snov_scraper import load_cookies, run_scraper

app = FastAPI(title="Company Scraper")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

_HERE = Path(__file__).parent


@app.get("/", include_in_schema=False)
async def ui():
    return FileResponse(_HERE / "index.html", media_type="text/html")


class ScrapeRequest(BaseModel):
    cookies:       str
    location:      str       = "Mumbai"
    industry:      str       = "Fastener"
    max_companies: int       = 15
    company_size:  list[str] = []


@app.post("/scrape", include_in_schema=False)
async def scrape(req: ScrapeRequest, request: Request):
    try:
        cookies = load_cookies(req.cookies)
    except Exception as exc:
        err_msg = str(exc)
        async def _err():
            yield f'data: {json.dumps({"type": "error", "data": err_msg})}\n\n'
        return StreamingResponse(_err(), media_type="text/event-stream")

    config = {
        "cookies":       cookies,
        "location":      req.location,
        "industry":      req.industry,
        "max_companies": req.max_companies,
        "company_size":  req.company_size,
    }

    # Thread-safe queue — scraper thread puts messages, SSE stream reads them
    msg_queue: stdlib_queue.Queue = stdlib_queue.Queue()

    def log_fn(msg: str):
        msg_queue.put({"type": "log", "data": str(msg)})

    def meta_fn(key: str, value):
        msg_queue.put({"type": "meta", "key": key, "value": value})

    def run_thread():
        # Create a fresh ProactorEventLoop in this thread — completely
        # independent of uvicorn's event loop, works on Python 3.14 Windows.
        if sys.platform == "win32":
            loop = asyncio.ProactorEventLoop()
        else:
            loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(run_scraper(config, log_fn, meta_fn))
            msg_queue.put({"type": "done", "data": result})
        except Exception as exc:
            msg_queue.put({"type": "error", "data": str(exc)})
        finally:
            loop.close()

    thread = threading.Thread(target=run_thread, daemon=False)
    thread.start()

    async def event_stream():
        idle_ticks = 0
        try:
            while True:
                if await request.is_disconnected():
                    return
                try:
                    item = msg_queue.get_nowait()
                    yield f"data: {json.dumps(item)}\n\n"
                    idle_ticks = 0
                    if item["type"] in ("done", "error"):
                        return
                except stdlib_queue.Empty:
                    await asyncio.sleep(0.1)
                    idle_ticks += 1
                    # Send SSE keep-alive comment every 15s to prevent proxy timeouts
                    if idle_ticks >= 150:
                        yield ": keep-alive\n\n"
                        idle_ticks = 0
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=4009, reload=False)
