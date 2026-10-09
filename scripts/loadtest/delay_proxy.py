"""TCP proxy that adds a fixed one-way delay in each direction (order preserved, delays overlap like a real network).
   python delay_proxy.py <listen_port> <target_port> <one_way_ms>"""
import asyncio, sys, time
listen, target, delay = int(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3]) / 1000


async def pump(reader, writer):
    q = asyncio.Queue()

    async def rd():
        try:
            while data := await reader.read(65536):
                q.put_nowait((time.monotonic() + delay, data))
        finally:
            q.put_nowait(None)

    async def wr():
        try:
            while (item := await q.get()) is not None:
                ts, data = item
                if (w := ts - time.monotonic()) > 0:
                    await asyncio.sleep(w)
                writer.write(data); await writer.drain()
        finally:
            writer.close()
    await asyncio.gather(rd(), wr(), return_exceptions=True)


async def handle(cr, cw):
    sr, sw = await asyncio.open_connection("127.0.0.1", target)
    await asyncio.gather(pump(cr, sw), pump(sr, cw), return_exceptions=True)


async def main():
    srv = await asyncio.start_server(handle, "127.0.0.1", listen)
    async with srv:
        await srv.serve_forever()

asyncio.run(main())
