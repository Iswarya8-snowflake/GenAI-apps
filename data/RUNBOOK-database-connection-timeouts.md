# Operational Runbook: Database Connection Timeouts and Pool Exhaustion

**Runbook ID:** RB-STORAGE-004  
**Component:** `tick-storage-gateway` / `zenith-timescaledb-cluster` / `PgBouncer`  
**Classification:** Low Latency Data Tier  
**Target Audience:** SRE, Database Administrators, Ingestion On-Call  

---

## Symptoms

- Ingestion gateways or query APIs log `ConnectionTimeoutException`, `PSQLException: Connection lease timeout`, or `504 Gateway Timeout`.
- Grafana alert `TickDbPoolSaturationAlert` fires: Connection acquisition latency > 500 ms (normal baseline: < 10 ms).
- Client applications report dropped tick write batches or delayed bar chart renders on front-end quant terminals.
- PgBouncer reports high client wait queues (`cl_waiting` > 50), while server connections (`sv_active`) reach pool limits.

---

## Root Cause

Common root causes for database connection timeouts include:
1. **Long-Running Unindexed Analytical Queries:** Heavy historical queries run by internal analysts or compliance workers holding locks and monopolizing worker threads during market hours.
2. **Catalog Lock Contention on Hypertables:** Auto-chunk creation or table maintenance (e.g. `reindex`, `vacuum freeze`, or compression jobs) contending with high-rate concurrent micro-inserts.
3. **Connection Leaks in Microservice Workers:** Client applications failing to return connections to the pool upon unhandled network errors or thread interrupts.
4. **PgBouncer Server Starvation:** Backend database server exhausted its `max_connections` or hit CPU saturation due to bad query plans.

---

## Diagnosis Steps

1. **Check PgBouncer Connection Pool Capacity:**
   Connect to PgBouncer admin console:
   ```bash
   psql -p 6432 -U pgbouncer -h lb-internal.zenithtick.net pgbouncer -c "SHOW POOLS;"
   ```
   *Key metrics:*
   - `cl_waiting` > 0: Clients are actively queuing for a server connection.
   - `sv_login` > 0: PgBouncer is slow establishing new connections to PostgreSQL.

2. **Identify Long-Running or Blocking Transactions:**
   Run on the primary database node:
   ```sql
   SELECT pid, usename, client_addr, now() - query_start AS duration, state, wait_event_type, wait_event, query
   FROM pg_stat_activity
   WHERE state != 'idle' AND (now() - query_start) > interval '5 seconds'
   ORDER BY duration DESC;
   ```

3. **Check for Active Lock Contention:**
   ```sql
   SELECT locktype, relation::regclass, mode, granted, pid 
   FROM pg_locks 
   WHERE NOT granted;
   ```

4. **Verify Host Resource Saturation:**
   Check database node CPU and IO wait:
   ```bash
   ssh db-primary.zenithtick.net "uptime && vmstat 1 5"
   ```
   If `%iowait` is > 25%, verify disk saturation via `iostat -xz 1 5`.

---

## Resolution

1. **Immediate Terminating of Rogue Queries:**
   Cancel individual slow analytical queries:
   ```sql
   SELECT pg_cancel_backend(<pid>);
   ```
   If process does not release locks within 10 seconds, forcefully terminate it:
   ```sql
   SELECT pg_terminate_backend(<pid>);
   ```

2. **Throttle Heavy Background / Batch Deployments:**
   Temporarily scale down non-critical query consumers:
   ```bash
   kubectl scale deployment quant-screener-backend --replicas=0 -n analytics
   ```

3. **Reload PgBouncer Configuration:**
   If connections appear hung or stale, soft-reload PgBouncer:
   ```bash
   psql -p 6432 -U pgbouncer -h lb-internal.zenithtick.net pgbouncer -c "RELOAD;"
   ```

4. **Scale Application Connection Pools:**
   If write volume has permanently increased, tune HikariCP on ingestion pods:
   - Edit ConfigMap `tick-storage-gateway-config`:
     ```yaml
     dataSource.maximumPoolSize: "200"
     dataSource.connectionTimeout: "15000"
     ```
   - Perform a controlled rolling rollout:
     ```bash
     kubectl rollout restart deployment tick-storage-gateway -n market-data
     ```

5. **Post-Incident Recovery Verification:**
   - Confirm `cl_waiting` returns to 0 in PgBouncer.
   - Confirm query acquisition latency in Grafana drops to < 10 ms.
   - Verify tick write lag drops back below 100 ms across all asset classes.

