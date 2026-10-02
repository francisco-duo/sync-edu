import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: list[T]
    total: int


async def fetch_all[T](
    fetch_page: Callable[[int], Awaitable[Page[T]]],
    *,
    page_size: int,
    concurrency: int,
) -> list[T]:
    """Lê todas as páginas. A 1ª é sequencial (revela o total); as demais, concorrentes.

    A ordem das páginas é preservada. `concurrency` limita as requisições simultâneas.
    """
    first = await fetch_page(1)
    total_pages = math.ceil(first.total / page_size)
    if total_pages <= 1:
        return list(first.items)

    semaphore = asyncio.Semaphore(concurrency)

    async def limited(page_number: int) -> Page[T]:
        async with semaphore:
            return await fetch_page(page_number)

    rest = await asyncio.gather(*(limited(n) for n in range(2, total_pages + 1)))
    items = list(first.items)
    for page in rest:
        items.extend(page.items)
    return items
