# Operational Runbook: Feed Handler High CPU, Packet Loss, and Thread Contention

**Runbook ID:** RB-FEED-012  
**Component:** `zenith-itch-feed-handler` / `zenith-binary-multicast-receiver`  
**Classification:** Ultra-Low Latency Ingestion Tier  
**Target Audience:** Low Latency Network Engineers, Colocation SRE, Market Data On-Call  

---

## Symptoms

- Alert `FeedHandlerPacketDropSpike` triggers: Packet loss rate > 0.001% on venue multicast channels.
- Exchange sequence gap alarms fire; TCP replay retransmissions spike (> 50 replay requests/min).
- Host CPU utilization on dedicated core sets (e.g., Cores 2-7) sticks at 100% with high software interrupt (`%si`) or high involuntary context switching.
- P99 parsing latency per tick increases from < 800 nanoseconds to > 25 microseconds.

---

## Root Cause

High CPU and packet drops on bare-metal feed handlers are typically caused by:
1. **Thread Core Affinity & Interrupt Pollution:** Network interface card (NIC) IRQs or unrelated OS worker threads scheduled on the same CPU cores pinned for lockless ring-buffer ingestion.
2. **CPU Throttling & Power States:** CPU dynamic frequency scaling governor falling into `powersave` or C-states/P-states down-clocking execution cores during intermittent market pauses.
3. **Socket Receive Buffer Exhaustion:** Kernel socket buffers (`SO_RCVBUF` / `rmem_max`) insufficiently sized to absorb microburst packet trains during high market volatility.
4. **Ring Buffer Desync / GC Pauses (if JVM/C# based):** Unintended heap allocations causing minor garbage collection cycles inside the hot-path parsing loop.

---

## Diagnosis Steps

1. **Verify Interface Multicast Drops and Socket Overflows:**
   Query kernel socket statistics:
   ```bash
   netstat -su | grep -E "packet receive errors|receive buffer errors|RcvbufErrors"
   ss -umpn -a | grep -E "itch|feed"
   ```
   For Solarflare / Onload kernel-bypass interfaces:
   ```bash
   onload_stackdump --all | grep -iE "drop|overflow|err"
   ```

2. **Inspect CPU Core Utilization and Context Switching:**
   ```bash
   mpstat -P ALL 1 3
   pidstat -w -p $(pgrep -f zenith-itch-feed-handler) 1 5
   ```
   *Expected:* Voluntary and involuntary context switches should both be close to zero (< 10/sec) when CPU isolation is correctly active.

3. **Check CPU Frequency Scaling and C-State Transitions:**
   ```bash
   turbostat --quiet --show Busy%,Bzy_MHz,IRQ,Pkg%pc2,CoreTmp --interval 2
   cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq
   ```
   Ensure frequencies are locked at maximum turbo/base clock without power drops.

4. **Verify Hardware Interrupt (IRQ) Affinity:**
   Ensure NIC RX/TX queues are not interrupting feed handler parsing cores:
   ```bash
   cat /proc/interrupts | grep -E "sf0|ens1|mlx5"
   ```

---

## Resolution

1. **Lock CPU Governors to Performance:**
   Force all host CPU cores into high-performance execution mode:
   ```bash
   for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
     echo performance | sudo tee $f
   done
   ```

2. **Re-Pin Worker Threads to Dedicated Isolated Cores:**
   Re-apply CPU affinity mask to ensure dedicated processing:
   ```bash
   # Pin feed handler main process to isolated cores 4,5,6,7
   sudo taskset -apc 4,5,6,7 $(pgrep -f zenith-itch-feed-handler)
   
   # Confirm isolation
   taskset -cp $(pgrep -f zenith-itch-feed-handler)
   ```

3. **Tune Kernel Network Buffers Immediately:**
   Expand socket buffers to maximum headroom:
   ```bash
   sudo sysctl -w net.core.rmem_max=67108864
   sudo sysctl -w net.core.rmem_default=67108864
   sudo sysctl -w net.ipv4.udp_mem="4096000 8192000 16777216"
   ```

4. **Failover to Redundant Hot-Standby Feed Handler (If Packet Drops Persist):**
   If the host hardware or NIC remains unresponsive:
   - Promote secondary colocation handler (`colo-ny4-fh03`):
     ```bash
     curl -X POST http://colo-ny4-fh03.zenithtick.net:8080/api/v1/feed/promote-primary
     ```
   - Demote degraded handler:
     ```bash
     curl -X POST http://colo-ny4-fh02.zenithtick.net:8080/api/v1/feed/step-down
     ```

5. **Post-Recovery Verification:**
   - Confirm multicast sequence gap counter ceases incrementing.
   - Confirm TCP replay rate drops back to 0.
   - Verify tick parse latency returns to sub-microsecond levels.

