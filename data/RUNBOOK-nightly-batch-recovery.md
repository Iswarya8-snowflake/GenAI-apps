# Operational Runbook: Remediation and Recovery of Failed Nightly Batch Pipelines

**Runbook ID:** RB-BATCH-007  
**Component:** `zenith-eod-batch-settlement` / Apache Airflow / Apache Spark  
**Classification:** Core Data Warehousing & Risk Tier  
**Target Audience:** Data Engineers, Analytics Platform SRE, Morning Shift On-Call  

---

## Symptoms

- Airflow DAG `dag_eod_market_replay_and_settlement` fails or stalls in `up_for_retry` state past 04:00 EST.
- Slack / PagerDuty alert: `CRITICAL: EOD Batch Pipeline SLA Breach (> 3 hours runtime)`.
- Downstream risk, margin calculation, and regulatory reporting partitions in object storage (`s3://zenith-historical-ticks/equity/dt=YYYY-MM-DD/`) are missing or incomplete.
- Spark driver logs report `SparkException: Job aborted due to stage failure`, `OutOfMemoryError`, or `ExecutorLostFailure (exit code 137 - OOM Killed)`.

---

## Root Cause

Nightly trading data batch failures commonly occur due to:
1. **Volumetric Data Spikes & Data Skew:** Heavy market events producing billions of tick updates, with heavily traded instruments concentrating disproportionate data into single shuffle partitions.
2. **Kubernetes Memory Limits (OOMKilled):** Spark executor pods exceeding their Kubernetes pod memory request/limit boundaries (`exit code 137`), causing Kubernetes node controllers to terminate them.
3. **Deadlocks or Lock Timeouts on Metadata Databases:** Concurrent schema migration jobs or metadata catalog updates locking partition tables in the Hive metastore or database catalog.
4. **Upstream Missing or Corrupt Partitions:** An incomplete raw market ingestion landing batch from colocation capture appliances leading to `FileNotFoundException` or corrupt Snappy/GZIP blocks during decompression.

---

## Diagnosis Steps

1. **Check Airflow DAG Execution State:**
   ```bash
   airflow dags state dag_eod_market_replay_and_settlement 2026-03-04T23:00:00
   airflow tasks list dag_eod_market_replay_and_settlement --tree
   ```
   Identify the exact failed task ID (e.g. `task_aggregate_vwap_and_compress_parquet`).

2. **Retrieve Spark Driver and Executor Logs from Kubernetes:**
   ```bash
   kubectl logs -n spark-jobs -l spark-role=driver --tail=300 | grep -iE "exception|error|failed|oom"
   ```
   If pods were evicted or killed:
   ```bash
   kubectl get pods -n spark-jobs -l app.kubernetes.io/name=spark --show-labels
   kubectl describe pod <failed-executor-pod> -n spark-jobs | grep -E "Reason|Exit Code|Terminated"
   ```

3. **Verify Upstream Source Data Completeness:**
   Check raw ingestion bucket partition size and file counts:
   ```bash
   aws s3 ls s3://zenith-raw-tick-ingest/raw/dt=$(date -d "yesterday" +%Y-%m-%d)/ --summarize --human-readable
   ```
   Ensure file count and total size match expected daily thresholds (> 100 GB, > 500 files).

4. **Inspect Spark Stage Skew on Spark History Server:**
   Navigate to `https://spark-history.zenithtick.net`:
   - Inspect the failed Stage.
   - Compare Median task duration vs Max task duration. If Max is > 10x Median, severe partition skew is present.

---

## Resolution

1. **Rerun Failed Pipeline with Scaled Memory and AQE Configuration:**
   If the job failed due to memory exhaustion or task skew:
   - Override the job parameters via Airflow CLI or UI variable overrides:
     ```bash
     airflow dags trigger dag_eod_market_replay_and_settlement \
       --conf '{
         "spark.executor.memory": "16g",
         "spark.executor.memoryOverhead": "4096m",
         "spark.sql.shuffle.partitions": "1600",
         "spark.sql.adaptive.enabled": "true",
         "spark.sql.adaptive.skewJoin.enabled": "true"
       }'
     ```

2. **Clearing Partial Outputs Prior to Re-Execution:**
   Avoid duplicate key or append corruption by purging temporary staging paths:
   ```bash
   aws s3 rm s3://zenith-historical-ticks/staging/dt=$(date -d "yesterday" +%Y-%m-%d)/ --recursive
   ```

3. **Manual DAG Step Restart in Airflow:**
   Clear task state to resume the pipeline from the failed step forward:
   ```bash
   airflow tasks clear -d -u \
     -s $(date -d "yesterday" +%Y-%m-%d) \
     -t task_aggregate_vwap_and_compress_parquet \
     dag_eod_market_replay_and_settlement
   ```

4. **Escalation and SLA Protection:**
   - If the rerun is not projected to finish before 06:30 EST (Pre-Market open):
     - Notify the Trading Operations desk on `#trading-ops-alerts`:
       `"EOD settlement batch delayed due to volume skew. Estimated completion 06:45 EST. Quant risk engines running on fallback 5-day rolling snapshot."`
     - Trigger fallback static risk parameters if automated trading models require hard start values by 07:00 EST.

5. **Post-Completion Validation:**
   - Verify success marker `_SUCCESS` exists in target S3 prefix:
     ```bash
     aws s3 ls s3://zenith-historical-ticks/equity/dt=$(date -d "yesterday" +%Y-%m-%d)/_SUCCESS
     ```
   - Trigger downstream consumer validation alert to unblock trading desks.

