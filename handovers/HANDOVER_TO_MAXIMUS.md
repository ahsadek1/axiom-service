# 🚀 PRIMUS ENHANCEMENT HANDOVER — FOR MAXIMUS

**From:** Axiom  
**To:** Maximus  
**Date:** 2026-06-02 22:15 EDT  
**Status:** Ready for Implementation

---

## Mission Brief

Build **Primus v1.0** — the central SQS orchestrator for Nexus trading system.

Primus is the connective tissue between OMNI (synthesis/decisions) and the 9 trading engines (execution). It:
- Routes messages to correct engine queue
- Manages circuit breakers (auto-degradation on failure)
- Enforces rate limits (per-engine + global)
- Tracks every message end-to-end (CHRONICLE audit)
- Scores engine health in real-time (0-100)
- Manages DLQ (dead-letter queue) for failed messages
- Sends alerts when system degrades

---

## Implementation Checklist

### Phase 1: Foundation (Week 1)
- [ ] Review `/Users/ahmedsadek/nexus/handovers/primus_enhancement_handover_20260602.zip`
- [ ] Extract and read README.md + PRIMUS_OVERVIEW.md
- [ ] Study ENGINE_CONFIG.json (rate limits, circuit breaker thresholds)
- [ ] Create SQS queues (9 engines + DLQ + control queues)
- [ ] Set up monitoring (CloudWatch + CHRONICLE)

### Phase 2: Core Logic (Week 2)
- [ ] Implement message router (OMNI → correct engine)
- [ ] Implement circuit breaker (auto-open/half-open/closed)
- [ ] Implement rate limiter (token bucket per engine)
- [ ] Implement health score calculator
- [ ] Implement DLQ message tracking

### Phase 3: Integration (Week 3)
- [ ] Connect to OMNI (receive synthesis results)
- [ ] Connect to engines (route messages, collect responses)
- [ ] Connect to CHRONICLE (audit trail)
- [ ] Connect to capital handler (pre-trade verification)
- [ ] Build dashboards (Grafana/Prometheus)

### Phase 4: Operations (Week 4)
- [ ] Test runbooks (DLQ recovery, circuit breaker reset)
- [ ] Load test (validate 500 msg/sec throughput)
- [ ] Failover drills (engine outage scenarios)
- [ ] Documentation & on-call procedures
- [ ] Production deployment

---

## Success Metrics

By end of v1.0:

- ✅ **99.5% message delivery** (only legitimate DLQ)
- ✅ **< 500ms P95 latency** (message to Alpaca)
- ✅ **< 2% error rate** (DLQ bound)
- ✅ **< 2 min recovery** from single engine failure
- ✅ **Zero capital drift** (verified at every step)
- ✅ **Full audit trail** (every message tracked in CHRONICLE)

---

## Key Design Decisions

**Why SQS over Kafka?**
- No operational overhead (AWS managed)
- Built-in DLQ support
- Simpler integration with Lambda/microservices
- Cost-effective for Nexus scale

**Why circuit breaker at Primus, not in engines?**
- Single point of control (don't need 9 circuit breakers)
- Fast degradation (don't wait for individual engines to fail)
- Better observability (all decisions logged centrally)

**Why token bucket for rate limiting?**
- Smooth bursty traffic (allows controlled spikes)
- Per-engine customization (different engines have different caps)
- Simple tuning (just adjust tokens/sec per config)

---

## Questions for Ahmed (from spec)

**Q1: Throttling Authority**  
Should Primus have authority to throttle new picks from OMNI if queues back up?
- Option A: Primus throttles (active backpressure)
- Option B: Primus alerts OMNI, let it decide (passive backpressure)

**Q2: Safe Loss Threshold**  
What's the acceptable error rate?
- Goal: < 2% is proposed
- Acceptable: Is 5% OK? 10%?

**Q3: Circuit Breaker Authority**  
Should Primus automatically trip circuit breaker?
- Option A: Auto-trip + alert (proactive)
- Option B: Alert only, let ops decide (conservative)

**Q4: Worker Scaling Aggressiveness**  
How fast should we add capacity?
- Option A: Immediate (queue > 100 → add worker)
- Option B: Gradual (queue > 500 → first worker, > 1000 → second)

---

## Handover Files

Location: `/Users/ahmedsadek/nexus/handovers/`

- **primus_enhancement_handover_20260602.zip** — Complete package
  - README.md (quick start)
  - PRIMUS_OVERVIEW.md (11 KB architecture)
  - ENGINE_CONFIG.json (per-engine thresholds)
  - Code templates (monitor, orchestrator, circuit breaker)
  - Runbooks (DLQ recovery, scaling, disaster recovery)
  - Database schemas (metrics, DLQ, circuit state)

---

## Integration Points (Verified)

✅ **OMNI** — Input: synthesis results | Output: execution status  
✅ **Engines (9)** — Receive: messages from queue | Send: execution results  
✅ **CHRONICLE** — Log: every message, every metric, every alert  
✅ **Capital Handler** — Pre-trade: verify capital | Post-trade: lock/unlock  
✅ **Alpaca API** — Submit orders | Receive confirmations  

---

## Related Systems (Already Deployed)

- **SQS Preflight Validator** (Axiom) — Runs 8:30 AM ET daily, validates all 9 engines
- **Capital Handler v1.0** (Cipher) — Pre-trade verification, position tracking
- **CHRONICLE** (Shared) — Audit database, metrics storage
- **Nexus Execution Engines (9)** — All operational and ready to consume

---

## Ahmed's Expectations

From conversation:
> "I would like for you to hand over a zip file to Maximus it is for Primus enhancement towards oversight of SQS"

**Axiom's understanding:**
- Maximus should own Primus implementation
- Focus on SQS queue management & engine oversight
- Build for production (not MVP/prototype)
- Plan for scaling (Nexus will grow)
- Integrate with existing systems (OMNI, engines, CHRONICLE, capital handler)

---

## When You're Ready

1. Extract the zip
2. Read PRIMUS_OVERVIEW.md (all architecture decisions explained)
3. Review ENGINE_CONFIG.json (thresholds for each engine)
4. Come back with implementation plan or questions

Ahmed is available for clarifications. Axiom is available for cross-checks (we can do independent code review under PRECISION MANDATE v1).

---

**Handover Status:** COMPLETE  
**Next Step:** Maximus extracts zip + starts Phase 1  
**Timeline:** 4 weeks to v1.0 production ready

Good luck, Maximus. Build something solid. 🚀

— Axiom
