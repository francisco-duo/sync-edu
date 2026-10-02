"""Sonda de vazão do cliente HTTP: httpx x aiohttp contra o mock-provedor.

Foi o experimento que apontou o gargalo do benchmark (veja docs/benchmark.md). Roda DENTRO da
rede do compose, para que o servidor e a rede não sejam o fator limitante:

  docker compose exec -T -u root sync-service pip install -q aiohttp     # só para o experimento
  docker compose exec -T sync-service python - < scripts/http_client_probe.py

Cada rodada cria 3.000 usuários no mock-provedor (POST /users) com N requisições simultâneas e
imprime requisições por segundo. É uma sonda indicativa (1 execução por ponto), não um teste
estatístico.
"""

import asyncio
import time

import aiohttp
import httpx

BASE = "http://mock-provedor:8000"
REQUESTS = 3000


def body(i: int) -> dict[str, str]:
    return {
        "external_id": f"X{i}",
        "email": f"x{i}@e.edu",
        "first_name": "A",
        "last_name": "B",
    }


async def reset() -> None:
    async with httpx.AsyncClient(base_url=BASE) as client:
        await client.post("/_admin/reset")


async def with_httpx(concurrency: int) -> float:
    gate = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(base_url=BASE) as client:

        async def one(i: int) -> None:
            async with gate:
                response = await client.post("/users", json=body(i))
                assert response.status_code == 201

        started = time.perf_counter()
        await asyncio.gather(*(one(i) for i in range(REQUESTS)))
        return REQUESTS / (time.perf_counter() - started)


async def with_aiohttp(concurrency: int) -> float:
    gate = asyncio.Semaphore(concurrency)
    connector = aiohttp.TCPConnector(limit=0)
    async with aiohttp.ClientSession(BASE, connector=connector) as session:

        async def one(i: int) -> None:
            async with gate, session.post("/users", json=body(i)) as response:
                assert response.status == 201
                await response.read()

        started = time.perf_counter()
        await asyncio.gather(*(one(i) for i in range(REQUESTS)))
        return REQUESTS / (time.perf_counter() - started)


async def main() -> None:
    for concurrency in (3, 5, 10, 25):
        await reset()
        httpx_rate = await with_httpx(concurrency)
        await reset()
        aiohttp_rate = await with_aiohttp(concurrency)
        print(
            f"simultâneas={concurrency:2d}  httpx={httpx_rate:6.0f} req/s  "
            f"aiohttp={aiohttp_rate:6.0f} req/s",
            flush=True,
        )
    await reset()


if __name__ == "__main__":
    asyncio.run(main())
