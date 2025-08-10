"""Test rate limiting consistency across multiple workers."""

import asyncio
import subprocess

import httpx
import pytest


class TestConcurrency:
    """Test rate limiting with multiple uvicorn workers."""

    @pytest.mark.asyncio
    async def test_rate_limit_consistency_across_workers(self):
        """Test that rate limits are enforced consistently across multiple workers."""
        # Create a simple test app
        app_code = '''
from fastapi import FastAPI, Depends
from pacer import Limiter, Rate, limit

app = FastAPI()
limiter = Limiter(redis_url="redis://localhost:6379")

@app.on_event("startup")
async def startup():
    await limiter.startup()

@app.on_event("shutdown")
async def shutdown():
    await limiter.shutdown()

# Low limit to easily trigger rate limiting
@app.get("/test", dependencies=[Depends(limit(Rate(5, "10s", burst=2), limiter=limiter))])
async def test_endpoint():
    import os
    return {"worker": os.getpid()}
'''

        # Write test app to temporary file
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write(app_code)
            app_file = f.name

        # Start uvicorn with multiple workers
        process = None
        try:
            process = subprocess.Popen(
                ["uv", "run", "uvicorn", f"{app_file[:-3].replace('/', '.')}:app",
                 "--host", "0.0.0.0", "--port", "8123", "--workers", "4"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            # Wait for server to start
            await asyncio.sleep(3)

            # Make concurrent requests from multiple clients
            async def make_requests(client_id):
                results = {"success": 0, "rate_limited": 0, "workers": set()}
                async with httpx.AsyncClient() as client:
                    for _ in range(10):
                        try:
                            response = await client.get("http://localhost:8123/test")
                            if response.status_code == 200:
                                results["success"] += 1
                                results["workers"].add(response.json()["worker"])
                            elif response.status_code == 429:
                                results["rate_limited"] += 1
                        except Exception:
                            pass
                        await asyncio.sleep(0.1)
                return results

            # Run multiple clients concurrently
            tasks = [make_requests(i) for i in range(3)]
            results = await asyncio.gather(*tasks)

            # Verify rate limiting worked across workers
            total_success = sum(r["success"] for r in results)
            total_limited = sum(r["rate_limited"] for r in results)
            unique_workers = set()
            for r in results:
                unique_workers.update(r["workers"])

            # Should have hit multiple workers
            assert len(unique_workers) > 1, "Should have hit multiple workers"

            # Total successful requests should be close to limit (5 + burst 2 = 7)
            # Allow some slack for timing
            assert 5 <= total_success <= 10, f"Expected 5-10 successful requests, got {total_success}"

            # Should have some rate limited requests
            assert total_limited > 0, "Should have some rate limited requests"

        finally:
            # Clean up
            if process:
                process.terminate()
                process.wait(timeout=5)
            import os
            os.unlink(app_file)

    @pytest.mark.asyncio
    async def test_rate_limit_isolation_by_ip(self):
        """Test that rate limits are isolated by client IP."""
        # Create a test app
        app_code = '''
from fastapi import FastAPI, Depends
from pacer import Limiter, Rate, limit

app = FastAPI()
limiter = Limiter(redis_url="redis://localhost:6379")

@app.on_event("startup")
async def startup():
    await limiter.startup()

@app.on_event("shutdown")
async def shutdown():
    # Clear Redis to avoid interference
    if limiter.storage.redis:
        await limiter.storage.redis.flushdb()
    await limiter.shutdown()

@app.get("/test", dependencies=[Depends(limit(Rate(2, "5s", burst=0), limiter=limiter))])
async def test_endpoint():
    return {"status": "ok"}
'''

        # Write test app
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write(app_code)
            app_file = f.name

        process = None
        try:
            # Start server
            process = subprocess.Popen(
                ["uv", "run", "uvicorn", f"{app_file[:-3].replace('/', '.')}:app",
                 "--host", "0.0.0.0", "--port", "8124"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            await asyncio.sleep(3)

            # Make requests with different X-Forwarded-For headers (simulating different IPs)
            async with httpx.AsyncClient() as client:
                # Client 1 - should get 2 successful requests
                for i in range(3):
                    response = await client.get(
                        "http://localhost:8124/test",
                        headers={"X-Forwarded-For": "192.168.1.1"}
                    )
                    if i < 2:
                        assert response.status_code == 200
                    else:
                        assert response.status_code == 429

                # Client 2 - should also get 2 successful requests (different IP)
                for i in range(3):
                    response = await client.get(
                        "http://localhost:8124/test",
                        headers={"X-Forwarded-For": "192.168.1.2"}
                    )
                    if i < 2:
                        assert response.status_code == 200
                    else:
                        assert response.status_code == 429

        finally:
            if process:
                process.terminate()
                process.wait(timeout=5)
            import os
            os.unlink(app_file)
