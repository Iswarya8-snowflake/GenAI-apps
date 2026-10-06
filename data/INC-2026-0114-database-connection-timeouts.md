# Incident Report: INC-2026-0114 - Tick DB Connection Pool Starvation During Market Open

**Incident ID:** INC-2026-0114  
**Date:** 2026-01-14  
**Severity:** P1 (Critical)  
**Platform:** ZenithTick Market Data Platform  
**Component:** `tick-storage-gateway` / `zenith-timescaledb-cluster`  
**Author:** Zenith Reliability Engineering  

---

## Symptoms

- At 09:30:05 EST (Market Open), client queries against the historical tick store experienced high error rates, with HTTP 504 and `PoolTimeoutException` thrown by `tick-storage-gateway`.
- Downstream algorithmic pricing engines and quant dashboard users reported stale quote warnings and missing 1-minute aggregate bars.
- Grafana alert `TickDbClientConnectionWaitTimeHigh` triggered at 09:30:45 EST, indicating average connection acquisition wait times exceeded 12,000 ms (normal baseline: < 15 ms).
- PostgreSQL/TimescaleDB active connection count spiked to maximum capacity (`max_connections = 1200`), with over 980 connections locked in `idle in transaction` or waiting on lock acquisition.

```log
2026-01-14T09:30:18.234Z [tick-storage-gateway-7b9ff-42xk] ERROR o.z.t.storage.DbConnectionPool: 
Connection lease timeout after 10000ms. Active: 150/150, Pending: 894, Idle: 0
org.postgresql.util.PSQLException: Connection to 10.240.12.88:5432 refused or timed out in pool
```

---

## Root Cause

An unindexed ad-hoc analytics query introduced in release `v3.18.2` by the automated compliance audit service was executed concurrently across 40 instrument universes at 09:30:00 EST. 

The query performed a sequential table scan across the hypertable partition for end-of-day order revisions while holding an open read transaction. When real-time volume surged at market open, write transactions attempting to update hypertable chunk catalog locks were queued behind the sequential read transactions. This caused connection pool exhaustion in PgBouncer and the application-level HikariCP pools, rapidly cascading across all ingestion and query microservices.

---

## Diagnosis Steps

1. **Verify Connection Pool Saturation on Ingress Gateways:**
   Inspect PgBouncer client and server pool saturation:
   ```bash
   psql -p 6432 -U pgbouncer -h lb-internal.zenithtick.net pgbouncer -c "SHOW POOLS;"
   ```
   Observed: `cl_waiting` exceeded 1,400 clients on the `zenith_tick_primary` database pool.

2. **Identify Long-Running and Blocking Queries in PostgreSQL:**
   Execute lock and activity inspection query:
   ```sql
   SELECT pid, now() - query_start AS duration, state, query 
   FROM pg_stat_activity 
   WHERE state != 'idle' 
   ORDER BY duration DESC 
   LIMIT 10;
   ```
   Identified 40 worker threads running:
   ```sql
   SELECT * FROM compliance_order_audit 
   WHERE trade_date = '2026-01-13' AND status = 'REVISED';
   ```
   Each query had been running for over 380 seconds without hitting indexes.

3. **Check Lock Dependency Trees:**
   ```sql
   SELECT blocked_locks.pid AS blocked_pid,
          blocking_locks.pid AS blocking_pid,
          blocked_activity.query AS blocked_statement,
          blocking_activity.query AS current_statement_in_blocking_process
   FROM pg_catalog.pg_locks blocked_locks
   JOIN pg_catalog.pg_stat_activity blocked_activity ON blocked_activity.pid = blocked_locks.pid
   JOIN pg_catalog.pg_locks blocking_locks 
       ON blocking_locks.locktype = blocked_locks.locktype
       AND blocking_locks.database IS NOT DISTINCT FROM blocked_locks.database
       AND blocking_locks.relation IS NOT DISTINCT FROM blocked_locks.relation
       AND blocking_locks.page IS NOT DISTINCT FROM blocked_locks.page
       AND blocking_locks.tuple IS NOT DISTINCT FROM blocked_locks.tuple
       AND blocking_locks.virtualxid IS NOT DISTINCT FROM blocked_locks.virtualxid
       AND blocking_locks.transactionid IS NOT DISTINCT FROM blocked_locks.transactionid
       AND blocking_locks.classid IS NOT DISTINCT FROM blocked_locks.classid
       AND blocking_locks.objid IS NOT DISTINCT FROM blocked_locks.objid
       AND blocking_locks.objsubid IS NOT DISTINCT FROM blocked_locks.objsubid
       AND blocking_locks.pid != blocked_locks.pid
   JOIN pg_catalog.pg_stat_activity blocking_activity ON blocking_activity.pid = blocking_locks.pid
   WHERE NOT blocked_locks.granted;
   ```

---

## Resolution

1. **Immediate Mitigation (09:36 EST):**
   - Terminated the blocking compliance audit processes directly on the primary database node:
     ```sql
     SELECT pg_terminate_backend(pid) 
     FROM pg_stat_activity 
     WHERE query LIKE '%compliance_order_audit%' AND state != 'idle';
     ```
   - Temporarily paused the `zenith-compliance-cron` deployment in Kubernetes:
     ```bash
     kubectl scale deployment zenith-compliance-cron --replicas=0 -n data-pipeline
     ```
   - Flushed stale connection leases across PgBouncer:
     ```bash
     psql -p 6432 -U pgbouncer -h lb-internal.zenithtick.net pgbouncer -c "RELOAD;"
     ```

2. **System Recovery (09:41 EST):**
   - Ingestion pipelines caught up within 180 seconds; connection wait times dropped back to 3.8 ms.
   - Verified tick ingestion rates returned to standard 450,000 ticks/sec across all US Equities feeds.

3. **Permanent Corrective Actions:**
   - Added compound index `CREATE INDEX CONCURRENTLY idx_audit_date_status ON compliance_order_audit (trade_date, status);`.
   - Updated `statement_timeout = 15000` (15s max) on the compliance application user role.
   - Redirected all non-real-time compliance workloads to dedicated read-only replicas (`tick-ro-replica-01.zenithtick.net`).

