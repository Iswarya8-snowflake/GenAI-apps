# Incident Report: INC-2026-0305 - Nightly Settlement and Tick Archival Batch Job Failure

**Incident ID:** INC-2026-0305  
**Date:** 2026-03-05  
**Severity:** P2 (Major)  
**Platform:** ZenithTick Market Data Platform  
**Component:** `zenith-eod-batch-settlement` / Apache Spark on Kubernetes  
**Author:** Zenith Data Engineering Operations  

---

## Symptoms

- PagerDuty notification triggered at 03:45 EST: `AirflowDagFailed: dag_eod_market_replay_and_settlement`.
- The nightly tick compression, volume-weighted average price (VWAP) calculation, and regulatory trade archival pipeline failed on step `task_aggregate_vwap_and_compress_parquet`.
- Downstream risk management systems scheduled for 06:00 EST morning prep could not locate the daily partition `s3://zenith-historical-ticks/equity/dt=2026-03-04/`.
- Risk officers and quantitative researchers were blocked from running pre-market margin calculations and backtest validation checks.

```log
2026-03-05T03:41:19.492Z [spark-executor-zenith-eod-batch-04] ERROR org.apache.spark.executor.Executor: 
Exception in task 184.0 in stage 14.0 (TID 3820)
java.lang.OutOfMemoryError: Java heap space
    at org.apache.spark.sql.execution.joins.HashedRelation$.apply(HashedRelation.scala:391)
    at org.apache.spark.sql.execution.joins.BroadcastHashJoinExec.prepareBroadcastRelation(BroadcastHashJoinExec.scala:189)
    at org.apache.spark.sql.execution.joins.BroadcastHashJoinExec.doExecute(BroadcastHashJoinExec.scala:142)
```

---

## Root Cause

On 2026-03-04, an unprecedented spike in retail equity options volume generated over 420 million individual quote update events—a 350% increase over the rolling 30-day average.

In the nightly batch job, a Spark SQL join between raw tick records and the instrument reference metadata table was configured to use `BroadcastHashJoin`. The automated Spark broadcast threshold (`spark.sql.autoBroadcastJoinThreshold`) permitted the execution engine to broadcast the full instrument metadata map into memory on every executor.

Combined with data skew on high-frequency meme stock tickers (single ticker partition exceeding 14 GB uncompressed), the executor memory limit (`spark.executor.memory=8g`) was overwhelmed, triggering JVM `OutOfMemoryError: Java heap space` and causing the driver to abandon the job after reaching max task retry limits.

---

## Diagnosis Steps

1. **Examine Airflow DAG Task Execution Logs:**
   Navigated to Apache Airflow task console:
   ```bash
   airflow tasks logs dag_eod_market_replay_and_settlement \
     task_aggregate_vwap_and_compress_parquet 2026-03-04T23:00:00
   ```
   Confirmed driver terminated with `SparkException: Job aborted due to stage failure: Task failed 4 times`.

2. **Inspect Spark History Server and Event Logs:**
   - Evaluated Stage 14 execution metrics: Task skew was severe. 98% of tasks completed within 45 seconds, while 4 tasks ran for > 42 minutes before failing with OOM.
   - Task `TID 3820` had processed 11.8 GB input data whereas median task processed 180 MB.
   - Memory allocation graph showed executor heap usage hit 8.19 GB / 8.0 GB configured.

3. **Verify Data Volume in Object Storage Landing Zone:**
   ```bash
   aws s3 ls s3://zenith-raw-tick-ingest/raw/dt=2026-03-04/ --summarize --human-readable
   ```
   Total size: 892 GB (usual size: ~220 GB).

4. **Review Driver Join Strategy Plan:**
   Inspected physical execution plan from driver logs:
   ```text
   == Physical Plan ==
   *(3) BroadcastHashJoin [symbol#42], [instrument_id#88], Inner, BuildRight
   :- *(3) Filter (isnotnull(symbol#42))
   :  +- Scan parquet raw_ticks
   +- BroadcastExchange HashedRelationBroadcastMode
      +- *(1) Scan parquet instrument_metadata
   ```
   The plan confirmed dynamic broadcast hash join was chosen without skew handling.

---

## Resolution

1. **Immediate Pipeline Rerun with Skew Mitigation (04:15 EST - 05:22 EST):**
   - Updated Spark submit parameters for the failed DAG run:
     - Increased executor memory and memory overhead:
       ```properties
       spark.executor.memory=16g
       spark.executor.memoryOverhead=4g
       spark.sql.shuffle.partitions=1200
       ```
     - Disabled automatic broadcast join to force a sort-merge join with skew hints enabled:
       ```properties
       spark.sql.autoBroadcastJoinThreshold=-1
       spark.sql.adaptive.enabled=true
       spark.sql.adaptive.skewJoin.enabled=true
       spark.sql.adaptive.skewJoin.skewedPartitionFactor=4
       spark.sql.adaptive.skewJoin.skewedPartitionThresholdInBytes=268435456
       ```
   - Triggered manual re-run of the failed task in Airflow:
     ```bash
     airflow tasks clear -s 2026-03-04 -t task_aggregate_vwap_and_compress_parquet \
       dag_eod_market_replay_and_settlement
     ```
   - Pipeline completed successfully in 67 minutes at 05:22 EST.

2. **Downstream Data Validation (05:30 EST):**
   - Verified row counts and Parquet checksums in destination bucket:
     ```bash
     aws s3 ls s3://zenith-historical-ticks/equity/dt=2026-03-04/ --recursive
     ```
   - Unlocked downstream risk calculations; pre-market trading risk checks completed before the 07:00 EST market prep deadline.

3. **Permanent Corrective Actions:**
   - Updated the Spark production templates in Helm charts with permanent adaptive query execution (AQE) and skew-join defaults.
   - Introduced dynamic executor scaling (`spark.dynamicAllocation.enabled=true`) allowing cluster expansion up to 128 executors during high-volume trading days.
   - Configured an interim volume alert at 20:00 EST to warn data engineering if daily tick volume exceeds 150% of the 30-day baseline before nightly jobs trigger.

