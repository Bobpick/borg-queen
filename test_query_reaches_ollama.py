"""Prove TRANSMIT actually POSTs to Ollama instead of hanging silently."""
import asyncio
import json
import unittest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import server as queen


class QueryReachesOllama(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.got = {}

        async def tags(_request):
            return web.json_response({"models": [{"name": "cogito:14b-16k"}]})

        async def chat(request):
            self.got["chat"] = await request.json()
            text = "MOCK SYNTHESIS: ultrasound notes found."
            if self.got["chat"].get("stream"):
                resp = web.StreamResponse(
                    status=200, headers={"Content-Type": "application/x-ndjson"}
                )
                await resp.prepare(request)
                await resp.write(json.dumps({"message": {"content": text}, "done": False}).encode() + b"\n")
                await resp.write(json.dumps({"done": True}).encode() + b"\n")
                await resp.write_eof()
                return resp
            return web.json_response({
                "message": {"role": "assistant", "content": text}
            })

        async def generate(request):
            self.got["generate"] = await request.json()
            return web.json_response({"response": "MOCK GENERATE"})

        fake = web.Application()
        fake.router.add_get("/api/tags", tags)
        fake.router.add_post("/api/chat", chat)
        fake.router.add_post("/api/generate", generate)
        self.ollama_runner = web.AppRunner(fake)
        await self.ollama_runner.setup()
        site = web.TCPSite(self.ollama_runner, "127.0.0.1", 0)
        await site.start()
        port = list(self.ollama_runner.sites)[0]._server.sockets[0].getsockname()[1]
        queen.OLLAMA_BASE_URL = f"http://127.0.0.1:{port}"
        queen.swarm.config["mode"] = "auto"

        self.queen_server = TestServer(queen.create_app())
        await self.queen_server.start_server()
        self.client = TestClient(self.queen_server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.queen_server.close()
        await self.ollama_runner.cleanup()

    async def test_post_query_calls_ollama_and_returns_answer(self):
        resp = await self.client.post("/api/query", json={
            "query": "what do we have on ultrasound technology?",
            "model": "cogito:14b-16k",
        })
        body = await resp.json()
        self.assertEqual(resp.status, 200, body)
        self.assertTrue(self.got.get("chat") or self.got.get("generate"),
                        "Ollama never received a generate/chat request — query did not reach the LLM")
        self.assertIn("ultrasound", (body.get("answer") or "").lower())

    async def test_stream_query_emits_sse_tokens(self):
        resp = await self.client.post(
            "/api/query",
            json={
                "query": "what do we have on ultrasound technology?",
                "model": "cogito:14b-16k",
                "stream": True,
            },
            headers={"Accept": "text/event-stream"},
        )
        self.assertEqual(resp.status, 200)
        raw = await resp.text()
        self.assertIn("text/event-stream", resp.headers.get("Content-Type", ""))
        compact = raw.replace(" ", "")
        self.assertIn('"event":"token"', compact)
        self.assertIn("ultrasound", raw.lower())
        self.assertTrue(self.got.get("chat"), "streamed query never reached Ollama")


if __name__ == "__main__":
    unittest.main()
