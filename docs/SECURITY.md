# Security Considerations

## Proxy and IP Extraction Security

### The Challenge

When your application sits behind proxies (load balancers, CDNs, reverse proxies), determining the real client IP becomes a security-critical operation. Incorrect configuration can lead to:

- **Rate limit bypass**: Attackers spoofing IPs to evade limits
- **IP spoofing**: Malicious users impersonating others
- **Rate limit exhaustion**: Attackers triggering limits for legitimate users

### Default Behavior (Secure)

By default, FastAPI Pacer uses the immediate client IP without trusting any proxy headers:

```python
limiter = Limiter(
    extractor=extract_ip()  # No trusted proxies
)
```

This is the most secure configuration but won't work correctly behind proxies.

## Configuring Trusted Proxies

### Basic Configuration

```python
from pacer.extractors import extract_ip

limiter = Limiter(
    extractor=extract_ip(
        trusted_proxies=["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
    )
)
```

### Security Rules

1. **Only trust proxies you control**
   - Never trust public IPs unless you control them
   - Be specific with CIDR ranges

2. **Use the most restrictive configuration**
   ```python
   # Bad: Too permissive
   trusted_proxies=["0.0.0.0/0"]  # NEVER DO THIS!
   
   # Good: Specific
   trusted_proxies=["10.0.1.0/24"]  # Only your subnet
   ```

3. **Understand the extraction order**
   - First public IP in X-Forwarded-For chain
   - Falls back to first IP if all are private
   - Direct client IP if not from trusted proxy

## Common Deployment Scenarios

### 1. Behind AWS ALB/ELB

```python
# AWS Load Balancers use private IPs
limiter = Limiter(
    extractor=extract_ip(
        trusted_proxies=["10.0.0.0/8"],  # Your VPC CIDR
        real_ip_header="X-Forwarded-For"
    )
)
```

### 2. Behind Cloudflare

```python
# Cloudflare published IP ranges (verify current list!)
CLOUDFLARE_IPS = [
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    # ... full list from cloudflare.com/ips
]

limiter = Limiter(
    extractor=extract_ip(
        trusted_proxies=CLOUDFLARE_IPS,
        real_ip_header="CF-Connecting-IP"  # Cloudflare-specific
    )
)
```

### 3. Behind Nginx

```python
# Nginx on same machine or private network
limiter = Limiter(
    extractor=extract_ip(
        trusted_proxies=["127.0.0.1", "::1"],  # Localhost only
        real_ip_header="X-Real-IP"  # Or X-Forwarded-For
    )
)
```

### 4. Kubernetes with Ingress

```python
# Kubernetes cluster network
limiter = Limiter(
    extractor=extract_ip(
        trusted_proxies=["10.0.0.0/8"],  # Cluster CIDR
        real_ip_header="X-Forwarded-For"
    )
)
```

## Header Spoofing Prevention

### The Attack

Without proper configuration, attackers can spoof headers:

```bash
# Attacker sends:
curl -H "X-Forwarded-For: 1.2.3.4" https://api.example.com

# If misconfigured, rate limits apply to 1.2.3.4, not attacker
```

### Prevention

1. **Only trust configured proxies**
   - The extractor only processes headers from trusted proxy IPs
   - Direct connections ignore proxy headers

2. **Validate proxy chain depth**
   ```python
   # For fixed proxy depth (recommended)
   def extract_ip_fixed_depth(depth: int = 1):
       def _extract(request: Request) -> str:
           xff = request.headers.get("X-Forwarded-For", "")
           ips = [ip.strip() for ip in xff.split(",")]
           
           # Take IP at exact depth
           if len(ips) >= depth:
               return ips[-depth]  # Count from right
           
           return request.client.host
       return _extract
   ```

3. **Use proxy-specific headers when possible**
   - `CF-Connecting-IP` for Cloudflare
   - `X-Real-IP` for Nginx
   - `True-Client-IP` for Akamai

## API Key Security

### Secure API Key Extraction

```python
from pacer.extractors import extract_api_key

# Use standard header
limiter = Limiter(
    extractor=extract_api_key(header_name="X-API-Key")
)
```

### Best Practices

1. **Use HTTPS only** - API keys in headers need encryption
2. **Rotate keys regularly** - Implement key rotation
3. **Hash keys in logs** - Never log raw API keys
4. **Rate limit by key AND IP** - Defense in depth

```python
from pacer.extractors import extract_combined, extract_ip, extract_api_key

# Combine API key with IP for double protection
limiter = Limiter(
    extractor=extract_combined(
        extract_api_key(),
        extract_ip()
    )
)
```

## User ID Security

### Secure User Extraction

```python
def get_user_id_from_jwt(request: Request) -> str | None:
    """Extract user ID from verified JWT."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    
    token = auth[7:]
    try:
        # Verify JWT signature first!
        payload = verify_jwt(token)  # Your JWT verification
        return payload.get("sub")  # User ID
    except InvalidTokenError:
        return None

limiter = Limiter(
    extractor=extract_user_id(
        user_id_func=get_user_id_from_jwt,
        fallback_to_ip=True  # Still limit non-authenticated requests
    )
)
```

### Important Notes

1. **Always verify authentication** before extracting user ID
2. **Consider fallback behavior** for unauthenticated requests
3. **Don't trust client-provided user IDs** without verification

## Redis Security

### Connection Security

```python
# Use Redis AUTH
limiter = Limiter(
    redis_url="redis://:password@localhost:6379/0"
)

# Use Redis TLS (Redis 6+)
limiter = Limiter(
    redis_url="rediss://localhost:6379"  # Note: rediss://
)
```

### Key Namespacing

Rate limit keys include app name to prevent collisions:

```
{app_name}:{scope}:{route}:{identity}
```

Configure unique app names in multi-tenant environments:

```python
limiter = Limiter(
    app_name="tenant_1",  # Unique per tenant
)
```

## Fail Mode Security

### Fail-Open vs Fail-Closed

```python
# Fail-Open (default): Allow on Redis failure
# Risk: Rate limits not enforced during outage
# Benefit: Service remains available
limiter = Limiter(fail_mode="open")

# Fail-Closed: Deny on Redis failure  
# Risk: Service unavailable during Redis outage
# Benefit: Rate limits always enforced
limiter = Limiter(fail_mode="closed")
```

Choose based on your security requirements:
- **Fail-Open**: For user-facing APIs where availability is critical
- **Fail-Closed**: For sensitive APIs where security is paramount

## Monitoring and Alerting

### Security Metrics to Monitor

1. **Sudden spike in rate limit hits**
   - May indicate attack or misconfiguration

2. **Redis connection failures**
   - Could be DoS attack on Redis

3. **Unusual IP patterns**
   - Many requests from single IP (bypass attempt)
   - Many IPs with single request (distributed attack)

### Logging Best Practices

```python
import logging
import hashlib

logger = logging.getLogger(__name__)

def hash_ip(ip: str) -> str:
    """Hash IP for privacy-compliant logging."""
    return hashlib.sha256(ip.encode()).hexdigest()[:16]

# Log rate limit events
logger.info(f"Rate limit exceeded for {hash_ip(client_ip)}")
```

## Security Checklist

- [ ] Configured trusted proxies correctly
- [ ] Using most specific CIDR ranges possible
- [ ] Validated proxy header configuration
- [ ] Using HTTPS for API key transmission
- [ ] Verifying authentication before user extraction
- [ ] Redis connection is secured (AUTH/TLS)
- [ ] Appropriate fail mode selected
- [ ] Monitoring rate limit metrics
- [ ] Not logging sensitive data (IPs, API keys)
- [ ] Regular security audits of configuration

## Reporting Security Issues

If you discover a security vulnerability in FastAPI Pacer, please report it to:
[security contact information]

Do not open public issues for security vulnerabilities.