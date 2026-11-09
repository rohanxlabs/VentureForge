# P2 benchmark scenarios

These scenarios are reproducible evaluation inputs for the project-scoped P2 workflow. They are research briefs, not research results. No live connector research or benchmark scoring has been performed for these scenarios. Unit tests use synthetic connector fixtures only.

## Reproduction protocol

For each scenario:

1. Create an active project and complete P1 using a clearly labeled benchmark Problem Frame.
2. Approve the latest P1 frame and linked decision.
3. Generate a P2 plan, inspect and approve it, then run with the same documented connector configuration and limits.
4. Save the project ID, plan/report artifact IDs, run/workflow IDs, command arguments, connector identities, timestamp, and whether each source/result was live, mocked, or synthetic.
5. Review all extracted passages against captured content. Record semantic interpretation separately from exact passage verification.
6. Score only after the report is reviewed. Leave unavailable dimensions unscored; never infer a score from synthetic fixtures.

Use a temporary database for repeatable test runs. Live runs require configured providers and may change as sources and search results change. Preserve the report and its source inventory as the benchmark snapshot.

## A. Landslide early warning in Northeast India

**Scope:** How warning information reaches exposed communities in one named Northeast Indian district during landslide events. Select the district, population, and observation period in the Problem Frame before running; do not generalize from one district to all of Northeast India.

**Questions to investigate:**

- Which official monitoring and warning mechanisms operate in the selected district and period?
- Which agencies issue warnings, through which channels, and with what documented geographic reach?
- What evidence exists about landslide occurrence, affected communities, impacts, and reporting coverage?
- What communication, monitoring, infrastructure, or response constraints are documented, and for which places and periods?
- Which local or operational alternatives are documented, and what evidence describes their use?

**Evidence requirements:** Official disaster-management and geological sources; dated incident or impact records with clear geographic scope; primary evaluations or peer reviewed studies on warning dissemination and response; local reporting only when its date, location, and original sourcing can be checked. Seek conflicting or null findings as well as documented limitations.

**Do not assume:** that warnings are absent, late, or ineffective; that communication coverage is uniformly poor; or that a reported event count measures true incidence. Keep event reporting and population impact denominators visible.

## B. Healthcare workflow: medication reconciliation at hospital discharge

**Scope:** The information handoff for adults with multiple long term medicines discharged from one defined type of acute care setting. This scenario evaluates workflow evidence, not clinical treatment recommendations.

**Questions to investigate:**

- Which roles collect, verify, and communicate the medication list at discharge?
- What documented discrepancies or information gaps occur, in which populations and settings?
- What interventions or tools are used to support reconciliation and follow up?
- What outcomes have been measured, over what period, and with what study limitations?
- What workload or access barriers are reported by patients and care teams?

**Evidence requirements:** Clinical guidelines and official safety material for process definitions; peer reviewed systematic reviews and primary studies for measured outcomes; patient or clinician accounts for workflow context. Capture study setting, sample, comparator, outcome definition, and date. Separate association from causal claims.

**Do not assume:** that a discrepancy caused harm, that one workflow fits all settings, or that published results apply to a different patient population. Do not provide patient specific medical advice.

## C. Developer tools: diagnosing intermittent CI test failures

**Scope:** How software teams identify and resolve intermittent test failures in a specified CI platform and repository type. Select a language, platform, and team size in the Problem Frame.

**Questions to investigate:**

- How do developers currently detect, reproduce, triage, and resolve intermittent failures?
- Which causes and failure patterns are documented for the selected platform or language?
- What existing CI features and third party tools support flaky test detection and diagnosis?
- What measured engineering time or delivery impact is reported, and how was it measured?
- Which adoption constraints, false positives, or maintenance costs are documented?

**Evidence requirements:** Official platform documentation and incident or reliability data where available; peer reviewed empirical software engineering studies; transparent tool documentation; developer reports for qualitative workflow context. Distinguish vendor claims, observational results, and controlled measurements.

**Do not assume:** that every intermittent failure is a flaky test, that retrying improves reliability, or that a reported productivity estimate generalizes across teams.

## Evaluation record

For each benchmark report, reviewers should record evidence of:

| Dimension | Review question |
|---|---|
| Source traceability | Does each evidence-backed finding point to a source in the same project run? |
| Passage validity | Does the cited passage exist in the captured content? |
| Question coverage | Are unanswered and partially addressed questions visible? |
| Uncertainty | Are assumptions, study limits, and scope limits retained? |
| Contradictions | Are materially conflicting sources shown? |
| Source quality | Are authority, recency, directness, geography, and independence discussed with caveats? |
| Report usefulness | Does the report give a reviewer a concrete basis for later P3 work without making the P3 decision? |

**Current evaluation status:** Not run against live sources. Synthetic unit fixtures verify software behavior but are not benchmark findings or scores. No human reviewed benchmark findings are available yet.
