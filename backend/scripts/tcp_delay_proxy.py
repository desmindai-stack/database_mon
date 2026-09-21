"""Ölçüm aracı: TCP bağlantısına sabit gecikme (RTT) ekleyen vekil (Faz 31 Commit 10a).

Canlıda worker (Railway) ile izlenen sunucu ve meta veritabanı (Supabase) ARASINDA ağ gecikmesi var; yerel
Docker'da yok. Örnekleyicinin tur süresi büyük ölçüde round-trip sayısı × gecikmedir, bu yüzden ölçümü
gecikmesiz almak canlıyı temsil etmez. Bu vekil ayrı bir SÜREÇTE çalışır — ölçülen sürecin olay döngüsünü
kalabalıklaştırmasın.

    python scripts/tcp_delay_proxy.py --rtt-ms 40 55460=55450 55461=55433
    python scripts/tcp_delay_proxy.py --rtt-ms 0 --stall-ms 2500 --stall-every-s 6 56100=55433   # periyodik aksama

`--stall-ms N --stall-every-s S`: her S saniyede bir TÜM trafik N ms durur (ağ aksaması). Sabit RTT'den farkı:
örnekleyici çoğu tur hızlı, ara sıra 2-3 saniyelik bir turla karşılaşır — canlıda görülen "ortalama 296 ms, en yavaş
1800 ms" örüntüsü. (Sabit RTT ≥ 1 sn'de örnekleyici bağlanamıyor bile: sunucu tarafı statement_timeout.)

Her `dinlenen=hedef` çifti için 127.0.0.1:dinlenen -> 127.0.0.1:hedef. Gecikme her yönde RTT/2.
Sıra korunur: veri parçaları kuyruğa alınıp zamanlarına göre yazılır, sıradaki parça bir öncekini beklemez
(yani gecikme birikmez, yalnızca ötelenir — gerçek bir hat gibi).
"""

from __future__ import annotations

import argparse
import asyncio
import time

#: Bu andan önce hiçbir parça iletilmez (periyodik aksama).
_STALL_UNTIL = [0.0]


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, delay: float) -> None:
    queue: asyncio.Queue = asyncio.Queue()

    async def drain() -> None:
        while True:
            due, chunk = await queue.get()
            if chunk is None:
                break
            wait = max(due, _STALL_UNTIL[0]) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            writer.write(chunk)
            await writer.drain()
        writer.close()

    task = asyncio.create_task(drain())
    try:
        while chunk := await reader.read(65536):
            await queue.put((time.monotonic() + delay, chunk))
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        await queue.put((0, None))
        await task


async def _handle(client_reader, client_writer, target_port: int, delay: float) -> None:
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", target_port)
    except OSError:
        client_writer.close()
        return
    await asyncio.gather(
        _pump(client_reader, upstream_writer, delay),
        _pump(upstream_reader, client_writer, delay),
        return_exceptions=True,
    )


async def _stalls(stall_ms: float, every_s: float) -> None:
    while True:
        await asyncio.sleep(every_s)
        _STALL_UNTIL[0] = time.monotonic() + stall_ms / 1000.0


async def main(rtt_ms: float, mappings: list[tuple[int, int]], stall_ms: float = 0, stall_every_s: float = 0) -> None:
    servers = [
        await asyncio.start_server(lambda r, w, t=target: _handle(r, w, t, rtt_ms / 2000.0), "127.0.0.1", listen)
        for listen, target in mappings
    ]
    print("hazır", flush=True)
    background = [asyncio.create_task(_stalls(stall_ms, stall_every_s))] if stall_ms and stall_every_s else []
    await asyncio.gather(*(s.serve_forever() for s in servers), *background)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rtt-ms", type=float, required=True)
    parser.add_argument("--stall-ms", type=float, default=0)
    parser.add_argument("--stall-every-s", type=float, default=0)
    parser.add_argument("pairs", nargs="+", help="dinlenen_port=hedef_port")
    args = parser.parse_args()
    pairs = [tuple(int(x) for x in p.split("=")) for p in args.pairs]
    asyncio.run(main(args.rtt_ms, pairs, args.stall_ms, args.stall_every_s))
