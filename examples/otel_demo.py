#!/usr/bin/env python3
"""
Demo app with full OpenTelemetry instrumentation for FastAPI Pacer.

Prerequisites:
    pip install opentelemetry-distro opentelemetry-exporter-otlp opentelemetry-instrumentation-fastapi

Run the OTel stack:
    docker-compose -f docker-compose.otel.yml up -d

Run this app:
    python examples/otel_demo.py

View traces:
    http://localhost:16686  (Jaeger UI)

View metrics:
    http://localhost:9090  (Prometheus)
    http://localhost:3000  (Grafana - admin/admin)
"""

import asyncio
import random
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.metrics import MeterProvider, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

from pacer import Limiter, Policy, Rate, compose, key_api_key, key_ip, limit, set_limiter

# Configure OpenTelemetry
resource = Resource.create({
    "service.name": "fastapi-pacer-demo",
    "service.version": "0.2.0",
})

# Set up tracing
trace_provider = TracerProvider(resource=resource)
trace_processor = BatchSpanProcessor(
    OTLPSpanExporter(endpoint="localhost:4317", insecure=True)
)
trace_provider.add_span_processor(trace_processor)
trace.set_tracer_provider(trace_provider)
tracer = trace.get_tracer(__name__)

# Set up metrics
metric_reader = PeriodicExportingMetricReader(
    OTLPMetricExporter(endpoint="localhost:4317", insecure=True),
    export_interval_millis=10000,  # Export every 10 seconds
)
meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter(__name__)

# Create custom metrics for business logic
request_counter = meter.create_counter(
    name="app_requests_total",
    description="Total application requests",
    unit="1",
)

request_duration = meter.create_histogram(
    name="app_request_duration_ms",
    description="Application request duration",
    unit="ms",
)


# Create OTel hooks for rate limiter
from pacer.otel import OTelHooks

otel_hooks = OTelHooks(
    service_name="fastapi-pacer-demo",
    include_request_attrs=True,
    include_policy_attrs=True,
)

# Initialize limiter with OTel hooks
limiter = Limiter(
    redis_url="redis://localhost:6379",
    on_decision=otel_hooks.on_decision,
    on_error=otel_hooks.on_error,
    expose_policy_header=True,
)

set_limiter(limiter)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage limiter lifecycle."""
    await limiter.startup()
    yield
    await limiter.shutdown()


app = FastAPI(
    title="FastAPI Pacer OTel Demo",
    description="Demonstrating OpenTelemetry integration",
    lifespan=lifespan,
)

# Instrument FastAPI for automatic tracing
FastAPIInstrumentor.instrument_app(app)


# Define different policies for testing
strict_policy = Policy(
    rates=[
        Rate(10, "1m"),
        Rate(2, "10s"),
    ],
    key="ip",
    name="strict",
)

normal_policy = Policy(
    rates=[Rate(100, "1m", burst=20)],
    key="ip",
    name="normal",
)

api_key_policy = Policy(
    rates=[Rate(1000, "1m")],
    key="api_key",
    name="api_key",
)

composed_policy = Policy(
    rates=[Rate(50, "1m")],
    key=compose(key_ip, key_api_key),
    name="composed",
)


@app.get("/")
async def root():
    """Unprotected endpoint for testing."""
    with tracer.start_as_current_span("process_root_request") as span:
        span.set_attribute("endpoint", "root")
        
        # Simulate some work
        await asyncio.sleep(random.uniform(0.01, 0.05))
        
        # Record business metric
        request_counter.add(1, {"endpoint": "root", "protected": "false"})
        
        return {"message": "Hello from OTel demo!"}


@app.get("/api/normal", dependencies=[Depends(limit(normal_policy))])
async def normal_endpoint(request: Request):
    """Normal rate limit endpoint."""
    with tracer.start_as_current_span("process_normal_request") as span:
        span.set_attribute("endpoint", "normal")
        span.set_attribute("client_ip", request.client.host if request.client else "unknown")
        
        # Simulate work
        duration = random.uniform(0.02, 0.1)
        await asyncio.sleep(duration)
        
        # Record metrics
        request_counter.add(1, {"endpoint": "normal", "protected": "true"})
        request_duration.record(duration * 1000, {"endpoint": "normal"})
        
        return {"endpoint": "normal", "policy": "100/min"}


@app.get("/api/strict", dependencies=[Depends(limit(strict_policy))])
async def strict_endpoint(request: Request):
    """Strict rate limit endpoint."""
    with tracer.start_as_current_span("process_strict_request") as span:
        span.set_attribute("endpoint", "strict")
        
        # Simulate work
        duration = random.uniform(0.05, 0.2)
        await asyncio.sleep(duration)
        
        # Record metrics
        request_counter.add(1, {"endpoint": "strict", "protected": "true"})
        request_duration.record(duration * 1000, {"endpoint": "strict"})
        
        return {"endpoint": "strict", "policy": "10/min, 2/10s"}


@app.get("/api/data", dependencies=[Depends(limit(api_key_policy))])
async def api_endpoint(request: Request):
    """API key protected endpoint."""
    with tracer.start_as_current_span("process_api_request") as span:
        api_key = request.headers.get("X-API-Key", "none")
        span.set_attribute("endpoint", "api")
        span.set_attribute("has_api_key", api_key != "none")
        
        # Simulate work
        duration = random.uniform(0.01, 0.05)
        await asyncio.sleep(duration)
        
        # Record metrics
        request_counter.add(1, {"endpoint": "api", "protected": "true"})
        request_duration.record(duration * 1000, {"endpoint": "api"})
        
        return {"endpoint": "api", "api_key_present": api_key != "none"}


@app.get("/api/composed", dependencies=[Depends(limit(composed_policy))])
async def composed_endpoint(request: Request):
    """Endpoint with composed selector (IP + API key)."""
    with tracer.start_as_current_span("process_composed_request") as span:
        span.set_attribute("endpoint", "composed")
        
        # Simulate work
        duration = random.uniform(0.03, 0.08)
        await asyncio.sleep(duration)
        
        # Record metrics
        request_counter.add(1, {"endpoint": "composed", "protected": "true"})
        request_duration.record(duration * 1000, {"endpoint": "composed"})
        
        return {"endpoint": "composed", "selector": "ip+api_key"}


@app.get("/health")
async def health():
    """Health check endpoint."""
    is_healthy = await limiter.is_healthy()
    return {
        "status": "healthy" if is_healthy else "unhealthy",
        "redis": "connected" if is_healthy else "disconnected",
    }


@app.get("/metrics")
async def get_metrics():
    """Prometheus metrics endpoint."""
    # Return both Prometheus client metrics and our custom metrics
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )


@app.get("/load-test")
async def load_test():
    """Generate load for testing observability."""
    with tracer.start_as_current_span("load_test") as span:
        results = {
            "normal": {"success": 0, "rate_limited": 0},
            "strict": {"success": 0, "rate_limited": 0},
        }
        
        # Generate 20 requests to each endpoint
        for i in range(20):
            # Test normal endpoint
            try:
                from httpx import AsyncClient
                async with AsyncClient() as client:
                    resp = await client.get("http://localhost:8000/api/normal")
                    if resp.status_code == 200:
                        results["normal"]["success"] += 1
                    elif resp.status_code == 429:
                        results["normal"]["rate_limited"] += 1
            except Exception as e:
                span.record_exception(e)
            
            # Test strict endpoint
            try:
                async with AsyncClient() as client:
                    resp = await client.get("http://localhost:8000/api/strict")
                    if resp.status_code == 200:
                        results["strict"]["success"] += 1
                    elif resp.status_code == 429:
                        results["strict"]["rate_limited"] += 1
            except Exception as e:
                span.record_exception(e)
            
            # Small delay between requests
            await asyncio.sleep(0.1)
        
        span.set_attribute("load_test.total_requests", 40)
        span.set_attribute("load_test.results", str(results))
        
        return results


if __name__ == "__main__":
    import uvicorn
    
    print("=== FastAPI Pacer OpenTelemetry Demo ===")
    print()
    print("Make sure OTel stack is running:")
    print("  docker-compose -f docker-compose.otel.yml up -d")
    print()
    print("Dashboards:")
    print("  Jaeger UI (traces):  http://localhost:16686")
    print("  Prometheus (metrics): http://localhost:9090")
    print("  Grafana (dashboards): http://localhost:3000 (admin/admin)")
    print()
    print("Test endpoints:")
    print("  curl http://localhost:8000/")
    print("  curl http://localhost:8000/api/normal")
    print("  curl http://localhost:8000/api/strict")
    print("  curl -H 'X-API-Key: test123' http://localhost:8000/api/data")
    print()
    print("Generate load:")
    print("  curl http://localhost:8000/load-test")
    print()
    
    uvicorn.run(app, host="0.0.0.0", port=8000)