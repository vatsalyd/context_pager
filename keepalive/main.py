import asyncio
import os
import httpx

PUBLIC_URL = os.getenv("PUBLIC_URL", "https://pager.duckdns.org")
CPU_TARGET = 0.05  # 5%
MEM_TARGET_GB = 5

# Allocate 5 GB resident memory
_resident = bytearray(MEM_TARGET_GB * 1024**3)


async def cpu_burn():
    """Busy loop calibrated to ~5% CPU on 4 OCPU."""
    while True:
        await asyncio.sleep(0.95)
        _ = sum(i * i for i in range(10000))


async def ping_loop():
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                await client.get(f"{PUBLIC_URL}/healthz")
            except Exception:
                pass
            await asyncio.sleep(120)


async def main():
    await asyncio.gather(cpu_burn(), ping_loop())


if __name__ == "__main__":
    asyncio.run(main())