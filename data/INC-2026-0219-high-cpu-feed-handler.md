# Incident Report: INC-2026-0219 - Feed Handler Core Saturation and Multicast Packet Drops

**Incident ID:** INC-2026-0219  
**Date:** 2026-02-19  
**Severity:** P1 (Critical)  
**Platform:** ZenithTick Market Data Platform  
**Component:** `zenith-itch-feed-handler` (Secaucus NY4 Colocation Host: `colo-ny4-fh02`)  
**Author:** Zenith Low Latency Infrastructure Engineering  

---

## Symptoms

- At 10:00:02 EST, during high volatility following an unexpected economic rate release, the primary ITCH market data feed handler `colo-ny4-fh02` reported UDP multicast socket packet drops.
- Market data gaps were observed on instrument feeds for Nasdaq Equities and US Treasury ETFs. Downstream trading engines were forced into gap recovery mode, requesting TCP historical tick replays.
- Solarflare Onload network stack counters reported `onload_stack_dump` socket drops: `rx_drop_count` jumped by 42,800 packets in under 60 seconds.
- CPU core utilization on pinned isolated cores (Cores 4-7) spiked to 100% with no idle cycles; process context switches jumped from ~200/sec to over 85,000/sec.

```log
2026-02-19T10:00:14.412093Z [colo-ny4-fh02:itch-worker-0] WARN  z.itch.receiver: 
Multicast sequence gap detected on Channel 3 (US-EQUITIES-A). Expected seq=19842109, Got seq=19842185. 
Gap size: 76 packets. Triggering TCP retransmission request to exchange replay server.
```

---

## Root Cause

A maintenance script executed the prior evening (`update_kernel_tuned.sh`) unintentionally altered the system `tuned` profile from `realtime-low-latency` to `throughput-performance`. 

This change modified several critical low-latency kernel runtime settings:
1. It disabled CPU isolation (`isolcpus`), allowing non-critical background OS daemons (including log rotation and Prometheus node-exporter) to be scheduled onto the dedicated ITCH parsing cores.
2. It re-enabled dynamic CPU frequency scaling (`scaling_governor=powersave` instead of `performance`), causing the CPU cores to down-clock during transient lulls and throttle when burst volume arrived.
3. Socket receive buffer sizes (`net.core.rmem_max` and `SO_RCVBUF`) were reset to Linux default values (212 KB instead of the configured 64 MB), resulting in immediate socket buffer overflows during microbursts.

---

## Diagnosis Steps

1. **Check Multicast Network Interface and Onload Drop Counters:**
   Ran Solarflare Onload diagnostic tool:
   ```bash
   onload_stackdump --all | grep -E "drop|pkt|rx_ring"
   ```
   Observed socket buffer queue full drops (`rx_overflow`) incrementing rapidly on interface `sf0`.

2. **Check Kernel CPU Frequency and Governor Settings:**
   ```bash
   cat /sys/devices/system/cpu/cpu[4-7]/cpufreq/scaling_governor
   cat /sys/devices/system/cpu/cpu[4-7]/cpufreq/scaling_cur_freq
   ```
   Reported: `powersave` and operating frequency fluctuating between 2.2 GHz and 3.1 GHz (baseline expected: locked at 4.5 GHz base frequency).

3. **Verify Thread Affinity and Context Switching:**
   Examined thread scheduling and context switching on isolated cores:
   ```bash
   pidstat -w -p $(pgrep -f zenith-itch-feed-handler) 1 5
   mpstat -P 4,5,6,7 1 3
   ```
   Showed non-voluntary context switches exceeding 15,000/sec per thread, indicating preemption by OS tasks.

4. **Review OS Tuned Profile State:**
   ```bash
   tuned-adm active
   ```
   Output showed: `Current active profile: throughput-performance` instead of `Current active profile: latency-performance`.

---

## Resolution

1. **Immediate In-Flight Remediation (10:08 EST):**
   - Locked CPU cores to maximum frequency immediately:
     ```bash
     for cpu in /sys/devices/system/cpu/cpu[0-15]/cpufreq/scaling_governor; do
       echo performance | sudo tee $cpu
     done
     ```
   - Expanded socket receive buffer via `sysctl`:
     ```bash
     sudo sysctl -w net.core.rmem_max=67108864
     sudo sysctl -w net.core.rmem_default=67108864
     ```
   - Reset the tuned profile to low-latency profile:
     ```bash
     sudo tuned-adm profile latency-performance
     ```
   - Restored thread affinity and isolcpus parameters via taskset:
     ```bash
     sudo taskset -cp 4,5,6,7 $(pgrep -f zenith-itch-feed-handler)
     ```

2. **Exchange Feed Resynchronization (10:12 EST):**
   - TCP retransmission queue was processed; book state was reconciled with venue snapshots.
   - Packet drops returned to zero; packet processing latency dropped from 120 microseconds to 3.8 microseconds.

3. **Permanent Corrective Actions:**
   - Locked `/etc/tuned/active_profile` with immutable flag and added automated configuration drift monitoring via Ansible check playbooks running every 15 minutes.
   - Added Datadog alerts for CPU scaling governor deviations (`cpu.governor != 'performance'`) and kernel context switch rate spikes on colocation bare-metal nodes.
   - Updated deployment runbooks to forbid running generic host maintenance scripts on live production market data nodes during trading weeks.

