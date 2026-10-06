# Incident Report: INC-2026-0322 - Internal mTLS Certificate Expiry Halting FIX Ingestion Gateway

**Incident ID:** INC-2026-0322  
**Date:** 2026-03-22  
**Severity:** P1 (Critical)  
**Platform:** ZenithTick Market Data Platform  
**Component:** `zenith-fix-gateway` / HashiCorp Vault PKI / Envoy Ingress  
**Author:** Zenith Security & Infrastructure Operations  

---

## Symptoms

- At 00:00:01 UTC, all institutional FIX drop-copy feeds and institutional REST market data API sessions disconnected abruptly.
- Upstream liquidity venues and institutional subscribers reported TLS handshake failures: `SSL_ERROR_CERT_EXPIRED` and `CERTIFICATE_VERIFY_FAILED`.
- Envoy ingress proxies across all Edge clusters logged TLS negotiation rejections on port 9800 (FIX over TLS) and port 443 (Market API).
- Automated health check alert `FixGatewayTlsHandshakeFailed` fired across London (LD4), New York (NY4), and Tokyo (TY3) data centers.

```log
2026-03-22T00:00:04.108Z [envoy-edge-ny4-ingress-3] [downstream] [error] 
[source/common/ssl/ssl_socket.cc:245] SSL error: 14094415:SSL routines:ssl3_read_bytes:sslv3 alert certificate expired
2026-03-22T00:00:04.110Z [zenith-fix-gateway-pod-12] WARN  z.fix.session.FixTlsSession: 
Peer 192.168.100.44 rejected connection: certificate validity expired at Sun, 22 Mar 2026 00:00:00 UTC
```

---

## Root Cause

The intermediate Certificate Authority (CA) certificate `ZenithTick-Cluster-IntCA-2024` and the associated leaf certificates issued by HashiCorp Vault reached their scheduled 2-year expiration timestamp at 2026-03-22 00:00:00 UTC.

While leaf certificates were configured with automated 30-day rotation via `cert-manager`, the root and intermediate CA bundles loaded into Envoy's static `secretDiscoveryService` (SDS) configuration were managed via a semi-automated Terraform workflow. 

A scheduled CA rotation ticket had been executed in the staging environment three weeks prior, but the production promotion pull request was left in an unmerged state due to an unresolved CI test flake. As a consequence, the expired intermediate CA remained active in production clusters.

---

## Diagnosis Steps

1. **Verify Ingress TLS Handshake Failures Externally:**
   Ran OpenSSL certificate check from external bastion:
   ```bash
   openssl s_client -connect fix-feed.zenithtick.net:9800 -servername fix-feed.zenithtick.net -showcerts
   ```
   Output:
   ```text
   depth=1 C = US, O = ZenithTick, CN = ZenithTick-Cluster-IntCA-2024
   verify error:num=10:certificate has expired
   notAfter=Mar 22 00:00:00 2026 GMT
   verify return:1
   ```

2. **Inspect HashiCorp Vault PKI Engine Status:**
   Queried Vault PKI secret engine:
   ```bash
   vault read pki_internal/cert/ca_chain
   vault read pki_internal/roles/trading-gateway-cert
   ```
   Discovered that Vault had already generated the new CA `ZenithTick-Cluster-IntCA-2026`, but the Kubernetes `Secret` resources had not synchronized.

3. **Check Cert-Manager and Secrets Synchronization in Kubernetes:**
   ```bash
   kubectl get certificates -n ingress-edge
   kubectl describe certificate zenith-ingress-tls -n ingress-edge
   ```
   Observed that `cert-manager` reported:
   `Issuing certificate as secret does not exist or has expired`, but the secret update was blocked by an admission webhook validating against the outdated intermediate CA bundle.

---

## Resolution

1. **Emergency Certificate Deployment (00:14 UTC - 00:26 UTC):**
   - Exported the valid 2026 CA chain and issued immediate replacement wildcard leaf certificates from Vault:
     ```bash
     vault write -format=json pki_internal/issue/trading-gateway-cert \
       common_name="*.zenithtick.net" ttl="720h" > /tmp/new_cert.json
     ```
   - Injected the updated TLS secret into the Kubernetes ingress namespace:
     ```bash
     kubectl create secret tls zenith-ingress-tls-emergency \
       --cert=/tmp/fullchain.pem \
       --key=/tmp/privkey.pem \
       -n ingress-edge --dry-run=client -o yaml | kubectl apply -f -
     ```
   - Patched Envoy Secret Discovery Service (SDS) config to point to the new emergency secret:
     ```bash
     kubectl patch deployment envoy-edge-ingress -n ingress-edge \
       --patch '{"spec":{"template":{"spec":{"volumes":[{"name":"tls-cert","secret":{"secretName":"zenith-ingress-tls-emergency"}}]}}}}'
     ```
   - Initiated a rolling restart of Envoy edge proxies across NY4, LD4, and TY3:
     ```bash
     kubectl rollout restart deployment envoy-edge-ingress -n ingress-edge
     ```

2. **Session Resumption & Verification (00:32 UTC):**
   - Verified TLS handshakes using OpenSSL:
     ```bash
     openssl s_client -connect fix-feed.zenithtick.net:9800 -brief
     ```
     Result: `CONNECTION ESTABLISHED`, `Verification: OK`.
   - FIX drop-copy clients reconnected and began re-transmitting lost fills. Full session continuity restored at 00:38 UTC.

3. **Permanent Corrective Actions:**
   - Overhauled CA rotation automation: Integrated Vault PKI directly with cert-manager's Vault issuer, removing manual intermediate CA synchronization.
   - Configured blackbox Prometheus `ssl_probe` metrics with alerting thresholds at 30 days, 14 days, and 48 hours prior to expiry for all certificate tiers (leaf, intermediate, and root).
   - Added automated linting to block deployment merges when unapplied infrastructure security changes remain open for more than 5 business days.

