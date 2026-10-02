import httpx

from sync_service.telemetry import Telemetry


def test_conta_chamadas_e_retries_por_sistema() -> None:
    telemetry = Telemetry()

    telemetry.count_call("provedor")
    telemetry.count_call("provedor")
    telemetry.count_call("academico")
    telemetry.retry_hook("provedor")(RuntimeError("x"))

    assert telemetry.snapshot() == {
        "http_calls": {"provedor": 2, "academico": 1},
        "retries": {"provedor": 1},
    }


def test_delta_mostra_so_o_que_mudou() -> None:
    telemetry = Telemetry()
    telemetry.count_call("provedor")
    before = telemetry.snapshot()

    telemetry.count_call("provedor")
    telemetry.count_call("provedor")
    telemetry.count_call("academico")

    assert Telemetry.delta(before, telemetry.snapshot()) == {
        "http_calls": {"provedor": 2, "academico": 1},
        "retries": {},
    }


async def test_gancho_conta_cada_requisicao_do_httpx_inclusive_as_que_falham() -> None:
    telemetry = Telemetry()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503 if request.url.path == "/falha" else 200)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://x",
        event_hooks={"request": [telemetry.request_hook("provedor")]},
    ) as client:
        await client.get("/ok")
        await client.get("/falha")

    assert telemetry.snapshot()["http_calls"] == {"provedor": 2}
