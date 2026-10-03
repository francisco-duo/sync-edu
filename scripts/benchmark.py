"""Benchmark reproduzível do edu-sync contra a stack do `docker compose`.

Cenários (todos medidos, nada é estimado):
  A0  dry-run da abertura do período (só calcula o plano)
  A   abertura do período: tudo é criado do zero
  B   execução diária sem mudanças (esperado: 0 ações)
  C   alterações diárias: novos alunos, saídas, trocas de turma e novas turmas
  B2  de novo sem mudanças depois do C (esperado: 0 ações)

Uso:
  docker compose up -d --build --wait
  python scripts/benchmark.py --label "padrao" --json-out docs/benchmark-results.json

Requer apenas `httpx`. As requisições são contadas pelos próprios mocks (/_admin/stats),
o que inclui retries e respostas 429/5xx. Os retries vêm da telemetria do sync-service.
"""

import argparse
import json
import os
import platform
import sys
import time
from typing import Any

import httpx

SYNC, ACADEMICO, PROVEDOR = "sync", "academico", "provedor"


class Bench:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.urls = {
            SYNC: args.sync_url,
            ACADEMICO: args.academico_url,
            PROVEDOR: args.provedor_url,
        }
        self.http = httpx.Client(timeout=httpx.Timeout(600.0, connect=5.0))

    # --- utilidades --------------------------------------------------------------------

    def get(self, service: str, path: str, **params: Any) -> dict[str, Any]:
        response = self.http.get(self.urls[service] + path, params=params)
        response.raise_for_status()
        return response.json()

    def post(self, service: str, path: str, json_body: Any = None, **params: Any) -> httpx.Response:
        return self.http.post(self.urls[service] + path, json=json_body, params=params)

    def stats(self, service: str) -> dict[str, Any]:
        return self.get(service, "/_admin/stats")

    def wait_healthy(self) -> None:
        for service, url in self.urls.items():
            for _ in range(60):
                try:
                    if self.http.get(url + "/health").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(1)
            else:
                sys.exit(f"{service} não respondeu em {url}/health")

    def wait_idle(self) -> None:
        while self.get(SYNC, "/sync-runs", status="running", dry_run="false")["total"]:
            time.sleep(0.2)

    # --- preparação --------------------------------------------------------------------

    def prepare(self) -> dict[str, Any]:
        args = self.args
        self.wait_healthy()
        self.wait_idle()
        summary = self.post(
            ACADEMICO,
            "/_admin/reset",
            {"students": args.students, "classes": args.classes, "seed": args.seed},
        ).json()
        self.post(PROVEDOR, "/_admin/reset")
        chaos = {
            "error_rate": args.provider_error_rate,
            "lose_response_rate": args.provider_lose_response_rate,
            "rate_limit_rps": args.provider_rate_limit_rps,
            "latency_ms": args.provider_latency_ms,
            "retry_after_seconds": args.provider_retry_after,
            "seed": args.seed,
        }
        self.http.put(self.urls[PROVEDOR] + "/_admin/chaos", json=chaos).raise_for_status()
        return summary

    # --- cenários ----------------------------------------------------------------------

    def measure(self, name: str, start: Any) -> dict[str, Any]:
        """`start()` dispara a sincronização e devolve o id do run; esperamos terminar."""
        before = {ACADEMICO: self.stats(ACADEMICO), PROVEDOR: self.stats(PROVEDOR)}
        t0 = time.perf_counter()
        run_id = start()
        while True:
            run = self.get(SYNC, f"/sync-runs/{run_id}")
            if run["status"] != "running":
                break
            time.sleep(0.1)
        wall = time.perf_counter() - t0
        after = {ACADEMICO: self.stats(ACADEMICO), PROVEDOR: self.stats(PROVEDOR)}
        return self.collect(name, run, wall, before, after)

    def collect(
        self,
        name: str,
        run: dict[str, Any],
        wall: float,
        before: dict[str, dict[str, Any]],
        after: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        def total(service: str) -> int:
            return after[service]["total"] - before[service]["total"]

        def by_status(service: str, status: str) -> int:
            return after[service]["by_status"].get(status, 0) - before[service]["by_status"].get(
                status, 0
            )

        metrics = run["metrics"]
        errors_5xx = sum(
            by_status(PROVEDOR, s) for s in after[PROVEDOR]["by_status"] if s.startswith("5")
        )
        writes = sum(
            after[PROVEDOR]["by_route"].get(route, 0) - before[PROVEDOR]["by_route"].get(route, 0)
            for route in after[PROVEDOR]["by_route"]
            if not route.startswith("GET")
        )
        return {
            "scenario": name,
            "status": run["status"],
            "wall_seconds": round(wall, 2),
            "actions": run["total_actions"],
            "actions_by_type": run["counters"],
            "succeeded": run["successful_actions"],
            "failed": run["failed_actions"],
            "skipped": run["skipped_actions"],
            "requests_provedor": total(PROVEDOR),
            "requests_provedor_writes": writes,
            "requests_academico": total(ACADEMICO),
            "responses_429": by_status(PROVEDOR, "429"),
            "responses_5xx": errors_5xx,
            "retries_provedor": metrics.get("retries", {}).get("provedor", 0),
            "retries_academico": metrics.get("retries", {}).get("academico", 0),
            "fetch_ms": metrics.get("fetch_ms"),
            "reconcile_ms": metrics.get("reconcile_ms"),
            "execute_ms": metrics.get("execute_ms"),
            "client_http_calls": metrics.get("http_calls", {}),
            "error": run["error"],
        }

    def real_run(self) -> Any:
        def start() -> str:
            response = self.post(SYNC, "/sync-runs")
            if response.status_code != 202:
                sys.exit(f"POST /sync-runs devolveu {response.status_code}: {response.text}")
            return response.json()["id"]

        return start

    def dry_run(self) -> dict[str, Any]:
        before = {ACADEMICO: self.stats(ACADEMICO), PROVEDOR: self.stats(PROVEDOR)}
        t0 = time.perf_counter()
        response = self.post(SYNC, "/sync-runs", dry_run="true")
        wall = time.perf_counter() - t0
        after = {ACADEMICO: self.stats(ACADEMICO), PROVEDOR: self.stats(PROVEDOR)}
        body = response.json()
        if response.status_code != 200:
            sys.exit(f"dry-run devolveu {response.status_code}: {response.text}")
        result = self.collect("A0 dry-run", body, wall, before, after)
        result["response_megabytes"] = round(len(response.content) / 1_000_000, 2)
        return result

    def consistent(self) -> dict[str, Any]:
        """O provedor realmente reflete o acadêmico? (contagens, não amostragem)"""
        school = self.get(ACADEMICO, "/_admin/summary")
        provider = self.get(PROVEDOR, "/_admin/summary")
        checks = {
            "usuarios_ativos": (provider["users_active"], school["students_active"]),
            "usuarios_suspensos": (provider["users_suspended"], school["students_inactive"]),
            "turmas": (provider["classes"], school["classes"]),
            "matriculas": (provider["memberships"], school["enrollments_active"]),
        }
        return {
            "ok": all(a == b for a, b in checks.values()),
            "detalhe": {k: {"provedor": a, "academico": b} for k, (a, b) in checks.items()},
        }

    def run_all(self) -> dict[str, Any]:
        args = self.args
        dataset = self.prepare()
        results = [self.dry_run()]
        results.append(self.measure("A abertura do período", self.real_run()))
        results[-1]["consistente"] = self.consistent()
        results.append(self.measure("B sem alterações", self.real_run()))
        results[-1]["consistente"] = self.consistent()

        changes = self.post(
            ACADEMICO,
            "/_admin/changes",
            {
                "new_students": args.new_students,
                "left_students": args.left_students,
                "moved_students": args.moved_students,
                "new_classes": args.new_classes,
                "seed": args.seed,
            },
        ).json()
        results.append(self.measure("C alterações diárias", self.real_run()))
        results[-1]["consistente"] = self.consistent()
        results.append(self.measure("B2 sem alterações (de novo)", self.real_run()))
        results[-1]["consistente"] = self.consistent()

        return {
            "label": args.label,
            "environment": {
                "platform": platform.platform(),
                "cpus": os.cpu_count(),
                "python_client": platform.python_version(),
            },
            "parameters": {
                "students": args.students,
                "classes": args.classes,
                "seed": args.seed,
                "daily_changes": {
                    k: changes[k]
                    for k in ("new_students", "left_students", "moved_students", "new_classes")
                },
                "provider_chaos": {
                    "error_rate": args.provider_error_rate,
                    "lose_response_rate": args.provider_lose_response_rate,
                    "rate_limit_rps": args.provider_rate_limit_rps,
                    "latency_ms": args.provider_latency_ms,
                    "retry_after_seconds": args.provider_retry_after,
                },
            },
            "dataset": dataset,
            "results": results,
        }


def print_markdown(report: dict[str, Any]) -> None:
    dataset = report["dataset"]
    print(f"### {report['label']}\n")
    print(
        f"Dataset: {dataset['students_active']} alunos, {dataset['classes']} turmas, "
        f"{dataset['enrollments_active']} matrículas ativas (seed {report['parameters']['seed']})\n"
    )
    print(
        "| Cenário | Status | Duração (s) | Ações | Requests provedor | Requests acadêmico | "
        "429 | 5xx | Retries | Falhas | Puladas |"
    )
    print("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in report["results"]:
        print(
            f"| {r['scenario']} | {r['status']} | {r['wall_seconds']} | {r['actions']} | "
            f"{r['requests_provedor']} | {r['requests_academico']} | {r['responses_429']} | "
            f"{r['responses_5xx']} | {r['retries_provedor'] + r['retries_academico']} | "
            f"{r['failed']} | {r['skipped']} |"
        )
    print()
    for r in report["results"]:
        parts = [f"fetch={r['fetch_ms']} ms", f"reconcile={r['reconcile_ms']} ms"]
        if r["execute_ms"] is not None:
            parts.append(f"execute={r['execute_ms']} ms")
        consistent = r.get("consistente", {}).get("ok")
        print(
            f"- {r['scenario']}: {', '.join(parts)}; ações por tipo: {r['actions_by_type']}"
            + ("" if consistent is None else f"; provedor == acadêmico: {consistent}")
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--sync-url", default="http://127.0.0.1:8000")
    parser.add_argument("--academico-url", default="http://127.0.0.1:8001")
    parser.add_argument("--provedor-url", default="http://127.0.0.1:8002")
    parser.add_argument("--label", default="padrão")
    parser.add_argument("--students", type=int, default=4000)
    parser.add_argument("--classes", type=int, default=90)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--new-students", type=int, default=100)
    parser.add_argument("--left-students", type=int, default=100)
    parser.add_argument("--moved-students", type=int, default=200)
    parser.add_argument("--new-classes", type=int, default=5)
    parser.add_argument("--provider-rate-limit-rps", type=int, default=0)
    parser.add_argument("--provider-latency-ms", type=int, default=0)
    parser.add_argument("--provider-error-rate", type=float, default=0.0)
    parser.add_argument("--provider-lose-response-rate", type=float, default=0.0)
    parser.add_argument("--provider-retry-after", type=int, default=1)
    parser.add_argument("--json-out", help="grava o relatório completo neste arquivo")
    args = parser.parse_args()

    report = Bench(args).run_all()
    print_markdown(report)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
