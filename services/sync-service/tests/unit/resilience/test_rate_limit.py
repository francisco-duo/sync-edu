import asyncio

import httpx
import pytest

from sync_service.resilience.rate_limit import AsyncRateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 6))


def limiter(rate: float, clock: FakeClock) -> AsyncRateLimiter:
    return AsyncRateLimiter(rate, clock=clock.monotonic, sleep=clock.sleep)


async def test_espaca_as_requisicoes_simultaneas_em_vagas_de_1_sobre_a_taxa() -> None:
    clock = FakeClock()
    rate_limiter = limiter(10, clock)  # 10 req/s -> uma vaga a cada 0,1 s

    await asyncio.gather(*(rate_limiter.acquire() for _ in range(5)))

    assert sorted(clock.sleeps) == [0.1, 0.2, 0.3, 0.4]  # a 1ª sai na hora


async def test_depois_de_ocioso_nao_acumula_credito_para_um_estouro() -> None:
    clock = FakeClock()
    rate_limiter = limiter(10, clock)
    await rate_limiter.acquire()
    clock.now += 60  # ficou 1 minuto parado

    await asyncio.gather(*(rate_limiter.acquire() for _ in range(3)))

    assert sorted(clock.sleeps) == [0.1, 0.2]  # a taxa continua valendo, sem rajada de 600


async def test_na_taxa_certa_nao_espera() -> None:
    clock = FakeClock()
    rate_limiter = limiter(10, clock)

    for _ in range(5):
        await rate_limiter.acquire()
        clock.now += 0.1

    assert clock.sleeps == []


@pytest.mark.parametrize("rate", [0, -1])
def test_taxa_invalida_e_recusada(rate: float) -> None:
    with pytest.raises(ValueError, match="positivo"):
        AsyncRateLimiter(rate)


async def test_gancho_do_httpx_passa_toda_requisicao_pelo_limitador() -> None:
    clock = FakeClock()
    rate_limiter = limiter(10, clock)
    transport = httpx.MockTransport(lambda request: httpx.Response(200))

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://x",
        event_hooks={"request": [rate_limiter.request_hook]},
    ) as client:
        for _ in range(3):
            await client.get("/a")

    # O relógio falso não avança: a 1ª sai na hora, a 2ª espera 0,1 s e a 3ª, 0,2 s.
    assert clock.sleeps == [0.1, 0.2]
