# Operational Runbook: Message Queue Backlog and Consumer Lag Mitigation

**Runbook ID:** RB-STREAM-009  
**Component:** `zenith-stream-bus` (Apache Kafka) / Downstream Consumer Services  
**Classification:** Real-Time Streaming Tier  
**Target Audience:** SRE, Streaming Platform Team, Market Data On-Call  

---

## Symptoms

- PagerDuty alert: `KafkaConsumerLagWarning` (> 100,000 unread messages) or `KafkaConsumerLagCritical` (> 1,000,000 unread messages).
- End-to-end tick transmission latency from exchange ingress to algorithmic book builders exceeds target SLAs (e.g., SLA > 5 ms).
- Downstream workers report increasing heap memory pressure, thread pool exhaustion, or constant consumer rebalance oscillations.
- Consumers fail to commit offsets within `max.poll.interval.ms`, generating warning: `CommitFailedException` or `Commit cannot be completed since the group has already rebalanced`.

---

## Root Cause

Market data queue backlogs typically stem from one of the following root causes:
1. **Volatile Market Surges:** Sudden news events causing message rates to spike 5x to 10x beyond the consumer group's baseline capacity.
2. **Slow Downstream Ingestion or Serialization:** Deserialization bottlenecks (e.g., complex Protobuf/JSON schema parsing) or blocking I/O calls inside worker processing loops.
3. **Partition Skew:** Uneven distribution of market ticks across Kafka partitions (e.g., heavily traded instrument tickers all hashed to a single partition).
4. **Consumer Rebalance Storms:** Heavy GC pauses or slow batch processing causing individual consumer instances to miss heartbeats, triggering rolling group rebalances where no work gets done.

---

## Diagnosis Steps

1. **Identify the Lagging Consumer Group and Partitions:**
   ```bash
   kafka-consumer-groups.sh \
     --bootstrap-server kafka-broker-01.zenithtick.net:9092 \
     --describe --group market-tick-aggregator-group
   ```
   Check columns:
   - `CURRENT-OFFSET` vs. `LOG-END-OFFSET`
   - `LAG` per partition
   - `CONSUMER-ID` (unassigned partitions show `-`)

2. **Check Consumer Group State:**
   ```bash
   kafka-consumer-groups.sh \
     --bootstrap-server kafka-broker-01.zenithtick.net:9092 \
     --describe --group market-tick-aggregator-group --state
   ```
   *Expect:* `Stable`. If `PreparingRebalance` or `CompletingRebalance`, the consumer group is flapping.

3. **Inspect Consumer Pod JVM and CPU Telemetry:**
   Check for CPU throttling or Stop-the-World garbage collection pauses on the worker pods:
   ```bash
   kubectl top pods -n market-data -l app=market-tick-aggregator
   kubectl logs --tail=200 -l app=market-tick-aggregator -n market-data | grep -iE "pause|gc|rebalance|timeout"
   ```

4. **Verify Broker-Side Network and Disk Health:**
   ```bash
   curl -s http://kafka-broker-01.zenithtick.net:9100/metrics | grep kafka_server_brokertopicmetrics_bytesin_total
   ```

---

## Resolution

1. **Immediate Horizontal Scaling (If Partitions > Current Pod Count):**
   If the topic has 32 partitions and only 16 pods are currently deployed, immediately increase replicas:
   ```bash
   kubectl scale deployment market-tick-aggregator --replicas=32 -n market-data
   ```

2. **Tune Batch Polling to Prevent Rebalance Storms:**
   If consumers are failing due to batch processing timeouts, reduce `max.poll.records` and increase timeout via Helm values or patch:
   ```bash
   kubectl set env deployment/market-tick-aggregator \
     MAX_POLL_RECORDS=500 \
     MAX_POLL_INTERVAL_MS=600000 \
     -n market-data
   ```

3. **Emergency Offset Reset (For Non-Durable Telemetry Feeds Only):**
   > [!CAUTION]
   > Do NOT execute on audit, trade fill, or compliance topics. Only use for transient L2 depth or quote streams where live data supersedes historical ticks.
   
   To drop stale backlog and resume live consumption:
   ```bash
   # Scale workers to 0 before resetting offsets
   kubectl scale deployment market-tick-aggregator --replicas=0 -n market-data
   
   # Reset consumer group offset to latest
   kafka-consumer-groups.sh \
     --bootstrap-server kafka-broker-01.zenithtick.net:9092 \
     --group market-tick-aggregator-group \
     --reset-offsets --to-latest \
     --topic market-ticks-l2-normalized \
     --execute
   
   # Restart workers
   kubectl scale deployment market-tick-aggregator --replicas=32 -n market-data
   ```

4. **Post-Mitigation Validation:**
   - Confirm lag drops to < 500 messages per partition within 5 minutes.
   - Confirm downstream order book consumers report latency < 2 ms.

