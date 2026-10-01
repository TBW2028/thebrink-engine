# The Brink World Physical-Risk Materiality & Confidence Methodology v1.0

**Methodology ID:** TBW-PRM v1.0  
**Materiality Rules:** TBW-MAT v1.0  
**Confidence Framework:** TBW-CONF v1.0  
**Evidence Schema:** TBW-EVID v1.0

## 1. Purpose

This methodology governs screening-level physical-risk intelligence produced by The Brink World for facility, pre-underwriting, due-diligence, business-continuity and monitoring workflows.

It is designed to keep source evidence, facility vulnerability, Brink interpretation, uncertainty and recommended actions distinct.

It does not determine insurance premium, insurability, probable maximum loss, regulatory capital, engineering adequacy or claims outcome.

## 2. Evidence chain

Every material conclusion should follow the same chain:

**Hazard evidence → Site exposure → Facility sensitivity → Critical function → Disruption pathway → Business consequence → Existing resilience → Materiality → Action**

No conclusion should imply more precision than the supporting evidence permits.

## 3. Materiality classes

### MATERIAL
Credible evidence supports a plausible physical-risk pathway capable of materially affecting the facility, a critical function, or the decision being assessed.

A Material classification does not mean a loss will occur.

### MONITOR
The hazard is relevant, but current evidence does not justify a Material conclusion. Continued monitoring, additional verification or periodic reassessment is appropriate.

### LOW RELEVANCE
Sufficient evidence indicates limited relevance to the assessed site and decision under the stated evidence, methodology and time horizon.

Low Relevance never means zero risk.

### EVIDENCE GAP
Evidence is insufficient, contradictory, unavailable, too coarse, stale, legally unusable, or missing a critical facility attribute needed for a defensible conclusion.

**Core rule: Unknown never becomes Low Relevance.**

## 4. Materiality gates

### Gate A — Evidence sufficiency
Confirm that an appropriate source exists, the metric matches the question, the spatial/temporal scale is fit for purpose, and use is permitted.

If not, classify as **Evidence Gap**.

### Gate B — Hazard relevance
Determine whether credible evidence identifies a relevant hazard or exposure at the site.

No meaningful exposure may support a candidate **Low Relevance** classification only when evidence quality is sufficient.

Ambiguous evidence should normally remain **Monitor** or **Evidence Gap**.

### Gate C — Facility sensitivity
Assess client-supplied or verified characteristics that can amplify or reduce consequence, including construction, basement/ground-floor exposure, critical equipment, backup power, water/cooling dependence and access redundancy.

### Gate D — Critical-function pathway
Identify whether the hazard can plausibly affect a critical business function through a traceable disruption pathway.

### Gate E — Resilience controls
Record existing controls separately from gross exposure. Client-reported controls remain client-reported unless independently verified.

## 5. Materiality triggers

A finding may be classified **Material** where credible evidence shows relevant exposure and one or more of the following are established:

- direct facility vulnerability;
- critical-function dependency;
- single-point access or utility dependency;
- significant historical site disruption;
- severe hazard requiring specialist determination;
- increasing future exposure combined with high facility sensitivity.

## 6. Confidence classes

Confidence describes how strongly the evidence supports the statement being made. It is **not** the probability that a hazard will occur.

### HIGH
Strong, appropriate, traceable evidence with limited material uncertainty for the statement.

### MEDIUM
Credible evidence supports the statement, but meaningful limitations remain.

### LOW
The statement relies materially on proxy, coarse, incomplete, weakly verified or assumption-heavy evidence.

### UNRESOLVED
Available evidence cannot support a defensible conclusion.

Confidence should consider:
- source authority;
- spatial fitness;
- temporal fitness;
- evidence agreement;
- facility-data quality.

## 7. Logical consistency

Allowed combinations include:
- Material + High / Medium / Low;
- Monitor + High / Medium / Low;
- Low Relevance + High / Medium / Low;
- Evidence Gap + Unresolved.

The system should not produce:
- Evidence Gap + High/Medium/Low;
- Low Relevance + Unresolved.

## 8. Evidence hierarchy

1. Authoritative local/national sources.
2. Authoritative international/scientific sources.
3. Established global risk datasets appropriate for screening.
4. Open mapping / secondary infrastructure evidence.
5. Brink interpretation.

Client information is separately classified as **Client Declared** or **Client Verified**.

Brink interpretation must never be represented as source evidence.

## 9. Conflicting evidence

Conflicting sources must be recorded rather than silently averaged or selectively discarded.

Where a material conflict cannot be resolved, the finding should remain Monitor, Low confidence or Evidence Gap/Unresolved depending on the nature of the conflict, and the report should identify the verification required.

## 10. Historical events

Absence of a recent event does not establish absence of long-term hazard.

Recent earthquake, flood, cyclone, fire or warning counts must not be used alone to infer future hazard or structural/financial loss.

## 11. Climate projections

Future climate evidence should identify:
- historical baseline period;
- future time horizon;
- scenario;
- dataset/model ensemble;
- central estimate;
- model spread/uncertainty;
- spatial resolution.

Interpretation should prioritise direction, magnitude, model agreement and facility sensitivity rather than false point precision.

## 12. Action types

- **VERIFY** — obtain or confirm missing evidence.
- **MONITOR** — continue evidence surveillance.
- **MITIGATE** — consider a practical resilience measure where evidence supports it.
- **SPECIALIST ASSESSMENT** — refer to qualified engineering, hydrology, geotechnical, insurance, fire-safety or other expertise.
- **ROUTINE REASSESSMENT** — revisit at the agreed cadence.

Automated outputs must not prescribe detailed engineering design.

## 13. Material change

A Material Change is a new development capable of altering a prior materiality classification, confidence level, operational consequence or required action.

Examples include:
- a new official severe warning;
- a significant hazard event near the facility;
- a new or revised hazard dataset;
- a facility vulnerability/control change;
- resolution of a prior Evidence Gap;
- a classification moving from Monitor to Material;
- a meaningful confidence change.

Routine weather variation should not trigger a Material Change alert unless it crosses defined operational thresholds.

## 14. Reproducibility

Every V2 report should retain, where available:
- facility profile version;
- report methodology version;
- materiality rules version;
- confidence rules version;
- evidence schema version;
- data-source versions;
- engine commit SHA;
- evidence retrieval timestamps;
- applicable Terms version.

Historical reports must retain the methodology under which they were generated.

## 15. Responsible boundary

The Brink World provides external physical-risk evidence, screening, monitoring and decision-support.

The methodology does not replace:
- structural engineering;
- site-specific hydrology;
- geotechnical assessment;
- fire-risk surveys;
- catastrophe-loss modelling;
- insurance pricing;
- statutory certification;
- local emergency instructions.

Where specialist evidence is necessary, the correct result is a clearly stated boundary and referral—not invented precision.
