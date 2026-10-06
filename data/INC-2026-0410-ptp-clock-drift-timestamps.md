# Incident Report: INC-2026-0410 - Precision Time Protocol (PTP) Clock Drift on Ingestion NICs

**Incident ID:** INC-2026-0410  
**Date:** 2026-04-10  
**Severity:** P2 (Major)  
**Platform:** ZenithTick Market Data Platform  
**Component:** `ptp4l` / `phc2sys` / Hardware Timestamping Engine (`colo-ld4-gw01`, `colo-ld4-gw02`)  
**Author:** Zenith Low Latency Infrastructure Engineering  

---

## Symptoms

- Regulatory compliance monitors triggered alert `MiFID_ClockDriftViolation_LD4` at 08:14 UTC, indicating internal exchange packet timestamps diverged from UTC reference time by > 124 microseconds (statutory threshold: < 100 microseconds for high-frequency algorithmic systems).
- Tick sequencing engines observed non-monotonic timestamps on matching engine fill reports originating from Equinix LD4 (Slough).
- Hardware timestamping telemetry on primary NIC `ens1f0np0` reported sudden synchronization loss to grandmaster clock (`gmIdentity 00:1b:21:ff:fe:a8:33:10`).
- Local system clock offsets reported by `phc2sys` oscillated between +85 us and -140 us.

```log
2026-04-10T08:14:02.109238Z [colo-ld4-gw01] ptp4l[14821]: [18294.201] port 1: MASTER_CLOCK_SELECTED 000000.0000.000000
2026-04-10T08:14:02.109244Z [colo-ld4-gw01] ptp4l[14821]: [18294.202] port 1: new foreign master 001b21.fffe.a83310-1
2026-04-10T08:14:02.312984Z [colo-ld4-gw01] phc2sys[14890]: [18294.414] CLOCK_REALTIME phc offset    124318 ns s2 freq  -128443 delay    1290
```

---

## Root Cause

An upstream fiber optical jumper degradation connected to the GPS antenna roof-receiver on the Equinix LD4 colocation facility caused intermittent loss-of-lock on the primary PTP Grandmaster Clock (GM-01). 

When GM-01 degraded, the secondary grandmaster (GM-02) was intended to assume active priority via the Best Master Clock Algorithm (BMCA). However, a recent firmware update on the top-of-rack boundary clock switch (`tor-sw-04a`) had an incorrect PTP domain profile configuration (`ptp-domain 0` instead of `ptp-domain 24`). 

As a result, when GM-01 stopped advertising valid GPS lock status, the ingestion host network interfaces (`colo-ld4-gw01`) fell into free-running holdover mode instead of locking onto GM-02. Within 8 minutes, crystal oscillator thermal drift caused the physical hardware clock (PHC) to drift beyond the 100-microsecond regulatory limit.

---

## Diagnosis Steps

1. **Verify PTP Daemon Tracking State and Port Status:**
   Queried the running `ptp4l` instance on the affected colocation hosts:
   ```bash
   pmc -u -b 0 -t 1 'GET TIME_STATUS_NP'
   pmc -u -b 0 -t 1 'GET CURRENT_DATA_SET'
   ```
   Observed `gmPresent: false` and `stepsRemoved: 0`, confirming the host was disconnected from active grandmasters.

2. **Examine Physical Hardware Clock (PHC) to System Clock Sync:**
   ```bash
   phc_ctl /dev/ptp0 get
   chronyc tracking
   ```
   Showed PHC offset continuously growing by approximately 18 ns per second against NTP backup.

3. **Check Boundary Switch PTP Status:**
   Logged into the Arista low-latency boundary clock switch:
   ```text
   tor-sw-04a# show ptp
   PTP Mode: Boundary Clock
   PTP Profile: IEEE 1588-2008 (Default)
   PTP Domain: 0  <-- INCORRECT (Production configured for Domain 24)
   Grandmaster Clock Identity: 00:00:00:00:00:00:00:00 (Holdover mode)
   ```

---

## Resolution

1. **Immediate PTP Domain Reconfiguration (08:21 UTC):**
   - Corrected PTP domain and priority configuration on boundary switch `tor-sw-04a`:
     ```text
     tor-sw-04a(config)# ptp domain 24
     tor-sw-04a(config)# ptp priority1 128
     tor-sw-04a(config)# exit
     ```
   - Restarted `ptp4l` and `phc2sys` on all colocation ingestion hosts:
     ```bash
     sudo systemctl restart ptp4l
     sudo systemctl restart phc2sys
     ```

2. **Verification of Lock Acquisition and Slew (08:24 UTC):**
   - Verified that `ptp4l` acquired lock on GM-02 within 18 seconds:
     ```bash
     pmc -u -b 0 -t 1 'GET TIME_STATUS_NP'
     ```
     Result: `gmPresent: true`, `offsetFromMaster: < 12 ns`.
   - `phc2sys` successfully slewed `CLOCK_REALTIME` back within bounds: offset stabilized at +9 ns.
   - Downstream timestamp validation and trade event sequencing returned to normal monotonic ordering.

3. **Permanent Corrective Actions:**
   - Deployed automated configuration auditing via Arista CloudVision to flag switch PTP profile parameter mismatches.
   - Configured high-frequency health checks in `telegraf` that raise an immediate P1 alert if PHC master offset exceeds 500 ns (long before approaching statutory 100 us limits).
   - Submitted a compliance incident notification report detailing the 10-minute drift window and audited all 412 trades executed during the window for accurate relative sequencing.

