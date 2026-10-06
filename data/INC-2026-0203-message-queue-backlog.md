# Incident Report: INC-2026-0203 - Kafka Consumer Lag Spike on Level-2 Depth Stream

**Incident ID:** INC-2026-0203  
**Date:** 2026-02-03  
**Severity:** P1 (Critical)  
**Platform:** ZenithTick Market Data Platform  
**Component:** `zenith-stream-bus` (Apache Kafka Cluster) / `depth-orderbook-worker`  
**Author:** Zenith Distributed Systems & Market Operations  

---

## Symptoms

- P99 feed delivery latency for normalized Level-2 (L2) market depth surged from 1.2 ms to over 48,000 ms between 14:00 EST and 14:28 EST.
- Real-time algorithmic market makers detected stale book states and automatically tripped circuit-breaker safety stops, temporarily disengaging quoting for European FX and Index Futures.
- Datadog alert `KafkaConsumerLagCritical: market-depth.normalized.v2` triggered across partitions 0 through 31, with cumulative unconsumed message lag exceeding 8.4 million messages.
- The `depth-orderbook-worker` consumer group entered a constant rebalance loop (rebalance storm), preventing steady state message consumption.

```log
2026-02-03T14:04:12.901Z [depth-orderbook-worker-prod-8] WARN  o.a.k.c.c.i.ConsumerCoordinator: 
[Consumer clientId=depth-worker-8, groupId=depth-orderbook-processor] 
consumer poll timeout has expired. This means the time between subsequent calls to poll() 
was longer than the configured max.poll.interval.ms of 300000 ms. Marking consumer as dead.
```

---

## Root Cause

During a heavy macro news release at 14:00 EST, market tick ingestion jumped from 600,000 msgs/sec to 2,900,000 msgs/sec. 

A newly deployed order book aggregation algorithm in `depth-orderbook-worker` (`v4.9.1`) executed an in-memory tree sort on every tick without a depth-cutoff limit. Under high-volatility conditions with deep order queues, processing each batch exceeded `max.poll.interval.ms` (300 seconds).

When worker pods failed to invoke `poll()` within the timeout window, Kafka marked them dead and triggered consumer group rebalances. As other consumers inherited the uncommitted partitions and encountered the same message backlog, they too timed out, producing a continuous rebalance loop across the entire consumer group.

---

## Diagnosis Steps

1. **Check Consumer Group Lag Across Partitions:**
   Run `kafka-consumer-groups` inspection against the cluster brokers:
   ```bash
   kafka-consumer-groups.sh \
     --bootstrap-server kafka-broker-01.zenithtick.net:9092 \
     --describe --group depth-orderbook-processor
   ```
   Output revealed massive lag (> 250,000 messages per partition) and fluctuating consumer IDs due to ongoing reassignments.

2. **Inspect Consumer Group State and Rebalance Metrics:**
   ```bash
   kafka-consumer-groups.sh \
     --bootstrap-server kafka-broker-01.zenithtick.net:9092 \
     --describe --group depth-orderbook-processor --state
   ```
   Status reported: `CompletingRebalance` oscillating to `PreparingRebalance`.

3. **Analyze Worker Pod CPU Profiling and GC Pauses:**
   Inspected JVM telemetry for `depth-orderbook-worker`:
   - JFR (Java Flight Recorder) snapshots revealed 74% of thread execution spent in `com.zenithtick.depth.OrderBook::recomputeFullDepth` inside red-black tree rebalancing.
   - Garbage collection logs indicated frequent Young Gen evacuations (1.8s pause every 4 seconds) due to high transient object allocation.

4. **Verify Upstream Broker Health and Disk I/O:**
   Broker disk write latencies remained healthy (< 2 ms), ruling out broker storage bottlenecks:
   ```bash
   ssh kafka-broker-01.zenithtick.net "iostat -xz 1 5"
   ```

---

## Resolution

1. **Immediate Mitigation (14:15 EST - 14:24 EST):**
   - Halted the crashing consumer group to stop rebalance oscillations:
     ```bash
     kubectl scale deployment depth-orderbook-worker --replicas=0 -n market-data
     ```
   - Temporarily patched worker configuration via ConfigMap:
     - Increased `max.poll.interval.ms` from 300,000 ms to 900,000 ms.
     - Reduced `max.poll.records` from 5,000 to 500 to bound batch processing times.
   - Scaled worker pool from 32 instances to 64 instances to match partition parallelism and handle backlog draining:
     ```bash
     kubectl scale deployment depth-orderbook-worker --replicas=64 -n market-data
     ```

2. **Restoring Live Order Book State (14:28 EST):**
   - Workers restarted successfully, established stable group assignments, and began consuming the backlog at a collective throughput of 4.2 million msgs/sec.
   - Backlog drained completely by 14:32 EST; downstream consumer latencies normalized to < 1.5 ms.

3. **Permanent Corrective Actions:**
   - Rolled back the unconstrained depth sorting code; capped real-time book depth rendering to top-50 levels with differential updates instead of full-tree recomputation.
   - Configured cooperative rebalance protocol (`org.apache.kafka.clients.consumer.CooperativeStickyAssignor`) to avoid stop-the-world partition revocations during group membership changes.
   - Implemented automated alerting on Kafka consumer commit frequency vs. poll threshold.

