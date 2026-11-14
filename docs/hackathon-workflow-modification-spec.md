# Hackathon Research Workflow: Project-Specific Modification Spec

**Status:** Implementation in progress - Challenge Setup and P1-P6 slices  
**Purpose:** Adapt VentureForge into a project-centered workflow that helps a team investigate a hackathon challenge, make evidence-backed decisions, build a focused MVP, and prepare a rubric-aligned submission.

## Product goal

VentureForge should guide a team from receiving a hackathon prompt to submitting a credible project. It should make the important decisions, evidence, constraints, and review points visible. It should not imply that generated research or a numerical score proves a problem or solution.

The reference diagram informs the use of review gates and feedback loops. It does not prescribe the product stages or require a linear process. The workflow below is organized around decisions and outputs while retaining the repository's existing P1-P8 stage records.

## Existing project inventory

| Existing capability | Code location | Reuse and limitation |
|---|---|---|
| Original startup discovery pipeline: pain-point mining, idea generation, scoring, pitch briefs, critique | `app.py`, `src/graph.py`, `src/agents/`, `src/state/` | Keep available as an optional idea-discovery tool. It is not the primary experience for an already assigned hackathon challenge and should not silently replace that challenge. |
| Project creation, project metadata, stage state/history, artifacts, decisions, approvals, workflow records | `src/project_store.py`, `src/research/storage.py`, `src/research/models.py` | Reuse as the project persistence foundation. P1-P8 statuses exist, but specialized behavior and UI coverage vary by stage. |
| P1 structured problem input, generated frame, versioning, human review and approval | `src/problem_intelligence.py`, `docs/problem-framing-p1.md` | Reuse. Extend intake/context to record the competition brief and explicitly separate organizer-provided facts, user-provided observations, assumptions, and unknowns. |
| P2 research plan, collection, captured sources, evidence, gap reporting, and project-owned report | `src/research/project_intelligence.py`, `src/research/`, `src/project_store.py`, `docs/research-intelligence-p2.md` | Reuse. Add explicit source quality/relevance review and support for primary research records. Desk research must remain distinct from user validation. |
| P3 assessment based on project research records | `src/problem_intelligence.py` | Change. The current deterministic assessment uses evidence count and question coverage to recommend `validated`; those measures do not establish that the problem is real or that users need the solution. P3 must capture a human decision and its rationale. |
| Solution design and pitch package generation | `src/research/solution.py`, `src/research/pitch.py`, `src/research_cli.py` | Reuse as generation services. Connect their inputs and outputs to project artifacts, stages, approvals, and competition rubric. |
| Existing stage names P1-P8 | `src/research/models.py` | Retain for continuity. Add Challenge Setup as project intake before P1 rather than shifting the established stage keys. |
| Main Gradio experience | `app.py` | Currently centered on startup discovery. Add a project workflow as the primary experience and keep startup discovery separate. |

## Target workflow and stage requirements

### Project intake: Challenge Setup (before P1)

**Purpose:** Understand what the event asks for and what the team is allowed and able to do.

Capture:

- Event name, official prompt, track, and links to source rules.
- Deadline, timezone, milestones, demo length/format, and submission format.
- Eligibility, required/allowed technologies, data/API rules, team-size constraints, and IP or licensing conditions.
- Judging criteria, weights, and any sponsor or track-specific requirements.
- Team members, skills, available time, access to mentors, and known technical dependencies.
- Whether the problem is assigned, bounded by a theme, or open-ended; identify which parts may be reframed.

**Output:** Versioned Challenge Brief with citations/links to rules, a normalized rubric, constraints, unresolved questions, and a completeness checklist. Allow manual entry and file/text import. Any extracted fields remain drafts until reviewed by a person. Do not assume rules from other competitions apply.

**Gate:** A person confirms the brief and requirements before the workflow recommends project directions. Missing or unclear rules remain visible as unknowns.

### P1 — Problem Framing

**Purpose:** Define the affected people and the specific difficulty, without jumping to a product idea.

Capture affected users, context, current workflow, frequency (if known), workarounds, impact, existing alternatives, scope, assumptions, and unknowns. Preserve the distinction between organizer claims, user/team statements, external evidence, and generated hypotheses.

**Output:** Versioned Problem Frame with a concise problem statement, target user, current process, known facts, hypotheses, unknowns, scope boundaries, and research questions.

**Gate and loop:** Human can approve, request changes, or reject the frame. Changes create a new version and require a fresh review. P1 approval accepts the framing for research; it does not validate the problem.

### P2 — Research

**Purpose:** Investigate the problem and its context through desk research, and plan practical first-hand research where possible.

Research areas should be selected based on the challenge and include relevant published evidence, affected-user context, current alternatives, prior attempts/solutions, market or operational context when applicable, and contradictory evidence. Plan interviews, observations, or lightweight tests where access and time permit.

**Output:** Versioned Research Plan and Research Report. Record the source, URL, retrieval time, relevant passage, question addressed, interpretation, and limitations. Record first-hand research separately: participant/context (using privacy-safe identifiers), method, prompts, raw notes or consented quotations, observation, interpretation, and follow-up. Do not treat an LLM summary or citation check as fact verification.

**Gate and loop:** Human reviews and approves the plan before collection, and reviews the report before P2 completion. Requested changes create a new plan/report version as appropriate. Unanswered questions and failed searches remain explicit.

### P3 — Problem Validation

**Purpose:** Make an explicit evidence-based decision about whether to continue with this problem and target user.

Present supporting evidence, contradicting evidence, source limitations, first-hand input, uncovered questions, and unresolved assumptions. Distinguish desk-research signals from observed or reported user behavior. Do not equate number of sources, question coverage, or a model-generated confidence with validation.

**Decision options:**

- **Proceed:** sufficient evidence for this hackathon's next step; record what is supported and what remains uncertain.
- **Gather more evidence:** define the specific gap, method, owner, and timebox; return to P2.
- **Reframe:** revise target user, context, or problem statement; return to P1 and preserve prior versions.
- **Stop / choose another challenge direction:** record the reason and any evidence that would change the decision.

**Output:** Human-authored Validation Decision with decision, rationale, evidence references, confidence/uncertainty explanation, and next action. A deterministic assistant can summarize the record and flag missing evidence, but cannot grant validation by threshold alone.

### P4 — Solution Intelligence

**Purpose:** Generate and compare candidate solution directions for a problem that passed the P3 decision gate.

Compare options on user/problem fit, evidence linkage, novelty relative to current alternatives, rubric fit, feasibility within remaining time and team skills, technical/data dependencies, risk, and a testable riskiest assumption. Include a baseline/no-build option where it helps compare value.

**Output:** Versioned Solution Options and a human-selected direction with rationale, assumptions, and a proposed experiment. Link claims to P1–P3 artifacts and competition criteria.

**Gate and loop:** Human selects, requests changes, or returns to P3/P1 if solution exploration exposes a problem in the framing or evidence. Selection does not mean the solution has been validated.

### P5 — Solution Engineering

**Purpose:** Turn the chosen direction into a credible, bounded implementation plan.

Capture the end-to-end user journey, architecture/data flow, integrations, dependencies, security/privacy/accessibility considerations where relevant, failure behavior, task breakdown, owners, and time estimates. Distinguish what will be real in the demo from mocked or simulated behavior.

**Output:** Versioned engineering plan and demo acceptance criteria. A human can revise scope before implementation.

### P6 — MVP Execution and Testing

**Purpose:** Build the smallest working slice that demonstrates the chosen value and tests the highest-risk assumption.

Track tasks, status, blockers, decisions, and evidence of completion. Define test scenarios and success signals before interpreting results. Store prototype/demo links, screenshots, test notes, known failures, and limitations. Support a build-measure-learn loop: test results may send the team to P4, P3, or P1.

**Output:** Runnable or demonstrable MVP, test/evaluation record, and a clear account of what is implemented, simulated, and not yet supported.

### P7 — Business, Impact, and Risk

**Purpose:** Explain who benefits, why the solution matters, and what would be required to use or continue it.

Capture the impact hypothesis and metric, adoption path, relevant stakeholders, operational/economic assumptions, sustainability or scaling considerations where appropriate, and risks/mitigations. Avoid invented market sizes or impact claims; label estimates and assumptions.

**Output:** Evidence-linked Impact and Feasibility Brief with measurement method and limitations.

### P8 — Pitch and Judge Preparation

**Purpose:** Prepare a submission and demo that make the project easy to evaluate against the actual event rubric.

For each criterion, show the project claim, supporting artifact or demo moment, remaining weakness, and score-improvement action. Prepare the required repository/readme, setup instructions, demo/video, screenshots, slide outline, timing, and likely judge questions only as required by the event. Include a fallback path for demo/network failure.

**Output:** Rubric-mapped submission checklist and pitch/demo package. Existing pitch generation can draft material, but project evidence and final claims require human review.

## Cross-stage behavior

- Keep versioned artifacts and append-only approval/decision history using existing project storage.
- Provide a stage timeline, current decision needed, evidence coverage, blockers, and a clear next action.
- Later work must reference the versions of earlier artifacts it used. If a referenced upstream artifact changes, mark downstream outputs as needing review; do not silently treat them as current.
- Support change requests and loops without deleting prior work.
- Allow a human to override a recommendation with a recorded rationale.
- Show what is known, inferred, assumed, generated, or still unknown. Never present generated text as independently verified research.
- Treat challenge-specific rules and rubric as authoritative inputs; generic guidance is secondary.
- Preserve the existing startup discovery flow as an optional tool, separate from working on a selected hackathon challenge.

## Product experience

The primary app path should be:

1. Project list / create project.
2. Challenge Setup and rubric confirmation.
3. Project workspace with a P1-P8 progress map, current decision, artifacts/evidence, and stage actions.
4. Stage workspaces containing focused inputs, generated drafts, evidence references, and review actions.
5. Project export containing artifacts, decisions, source references, and final submission materials.

Avoid presenting P1-P8 as eight empty checklist forms. Each stage should explain the decision it supports, what evidence is needed, and what output unlocks the next action.

## Data and service changes

1. Extend project intake data for competition details and the rubric without breaking existing project records.
2. Add first-hand research records separately from web-source research records, with privacy-aware participant references and explicit consent/recording fields where applicable.
3. Replace P3 threshold-based validation recommendations with a human decision record. Keep computed summaries descriptive (counts, coverage, source mix) rather than prescriptive.
4. Add project-owned P4/P5/P6/P7/P8 artifact and workflow services, reusing existing decisions, artifacts, and workflow-run APIs.
5. Link every stage output to its source project, upstream artifact versions, and applicable rubric criteria.
6. Use migrations or compatibility-safe schema extensions; preserve current research runs and existing projects.

## Delivery sequence

### Phase A — Research-ready project foundation

Build Challenge Setup, rubric/constraint capture, project dashboard, stage map, and a project-level evidence/decision view. Ensure user-approved intake is persisted as a versioned artifact.

**Implemented in the initial slice:** project creation/selection in the Gradio workspace; structured capture of the event prompt, challenge type, track, official links, deadline/timezone, team size/skills/time, technical constraints, allowed resources, submission/demo requirements, judging criteria, and unresolved rules; weighted-rubric total validation when all weights are supplied; versioned Challenge Brief artifacts with a linked review decision; atomic human approval/change-request/rejection history; a brief review summary; and a P1 start action gated on approval. The existing startup-discovery UI remains available below the project workspace. Existing projects without a Challenge Brief retain the legacy CLI behavior.

The initial slice uses manual entry and source URLs. Rule-page extraction, automated completeness checks, a full stage map, and a project-wide evidence/decision view remain future Phase A work.

### Phase B — Problem evidence and decision loop

Connect P1–P2 to the project UI, add first-hand research records, and redesign P3 as the explicit human decision described above. Make reframe and gather-more-evidence paths usable from the decision screen.

**Implemented in the current slice:** P1 Problem Framing is available under the project selector. It captures user/context details, current workflow, pain points, alternatives, measured and qualitative impacts, causes as hypotheses, constraints, assumptions, and unknowns. The approved Challenge Brief is carried as context and event constraints are included, while the UI labels organizer text as context rather than independent user evidence. P1 creates versioned frames, supports loading a saved frame for revision, records human approval/change requests/rejections, and gates P1 on an approved Challenge Brief. Frame approval completes P1; requesting changes returns P1 to in-progress. The deterministic frame generator does not collect research or validate the problem.

**Implemented in the current slice:** P2 Research is available for the selected project. It drafts an editable research plan, requires human plan approval before collection, uses the existing bounded desk-research services, and saves a versioned report with source traces, findings, gaps, and collection failures. The report has a separate human review gate before P2 completion. Live research execution requires a configured LLM for evidence extraction; Tavily is optional, and without it the available search coverage is limited. This P2 slice does not yet collect primary interviews or user-test records.

**Implemented in the current slice:** P3 Problem Validation presents the latest approved P1 frame and P2 report, including source-linked evidence IDs, research questions, and unanswered questions. A human records supporting and contradicting evidence references, first-hand observations, limitations, uncertainty, rationale, and next action, then chooses proceed, gather more evidence, reframe, or stop. Decisions are versioned and reference the exact upstream artifact versions. Gather-more-evidence reopens P2 and requires a method, owner, and timebox; reframe reopens P1; proceed and stop complete P3. The legacy CLI assessment now reports only that human review is required and cannot complete P3.

**Implemented in the current slice:** P4 is gated on a current P3 proceed decision and the latest approved P1/P2 artifacts. It provides a starter comparison draft for two or more distinct directions. Each direction records target user/problem fit, verified evidence IDs, a map to every event judging criterion, feasibility against team constraints, skills, dependencies, risks, assumptions, an MVP boundary, and an experiment for its riskiest assumption. The human can select a direction and record rationale, experiment owner, and timebox, or return to P3/P1. Selection completes P4; there is no score-based auto-selection. Options and decisions are versioned project artifacts and link back to the exact P1-P3 versions.

**Implemented in the current slice:** P5 opens only after P4 has a selected direction. The engineering plan captures a user journey, architecture and data flow, integrations, security/privacy/accessibility considerations, failure behavior, scope boundary, task dependencies, owners, person-hour estimates, event-rubric mapping, and demo/test acceptance criteria. Tasks and integrations label what is real, mocked, simulated, or deferred. Capacity is compared with the event team/time inputs as a planning warning. Plans reference the exact P1-P4 versions, are versioned, and require human approval; requested changes reopen P5, and direct stage completion is blocked without an approved latest plan.

**Implemented in the current slice:** P6 seeds a versioned execution board from the approved P5 task list, test scenarios, and demo acceptance criteria. The team records task status, blockers, completion evidence or deferral reasons, test environment and observed results, failure notes, demo links, and limitations. Dependency order is checked, every planned test/check must have a result before completion, and task/test records must match the current approved P5 version. Failed tests may remain visible when their failure and limitation are documented and the human reviewer accepts the execution record. P6 approval is an explicit review gate; requesting changes reopens the stage, and direct completion is blocked without the latest reviewed execution record.

### Phase C — Solution choice and testable MVP

Connect solution options to P4, create P5 engineering plans, and add P6 task/test tracking and evidence capture. Implement upstream-change warnings and feedback links.

### Phase D — Impact and submission readiness

Add P7 impact/feasibility records and connect P8 pitch generation to the event rubric, required deliverables, demo, and evidence. Keep existing pitch generation available where it is useful.

## Acceptance criteria

- A user can create a project, enter a challenge brief and actual event rubric, and see unresolved requirements before choosing a direction.
- A user can distinguish event-provided context, direct user input, external sources, generated hypotheses, and unknowns throughout the project.
- P1 approval does not label the problem validated.
- P2 research records source traces and shows collection failures, evidence gaps, and contradictions.
- P3 requires an explicit human decision and supports proceed, gather evidence, reframe, and stop paths with rationale and evidence references.
- A P4 solution option links back to the problem evidence and forward to a falsifiable test.
- P6 shows what works in the MVP and records test results against previously defined success signals.
- P8 maps submission claims to the event rubric and links claims to project artifacts or demo evidence.
- Existing projects, research history, P1/P2 workflows, CLI, and startup discovery remain usable during the transition.

## Open product decisions

- Whether Challenge Setup should parse uploaded rules automatically in the first release or begin with structured manual entry and URL references.
- Which primary research methods to support first: interview, observation, survey, or prototype test.
- Whether team/task assignment and deadlines are required for the first usable release or can follow after the evidence workflow.
- How to support hackathons where pre-event project work is restricted; event rules must control the allowed workflow.

