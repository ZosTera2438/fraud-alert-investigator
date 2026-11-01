# 0004. Strands Graph with code nodes, interrupts and session persistence

Status: accepted (2026-10-31)

## Decision

- Orchestration is a Strands `Graph`: intake, investigator, scoring, decision, action. Investigation order is known in advance, so a Swarm would add autonomy without benefit and make the run harder to audit.
- Intake, scoring and action are `CodeNode`s: small classes that satisfy Strands' `AgentBase` protocol, so deterministic steps sit in the same graph, trace and result as the agents.
- The policy guard's single retry happens inside the decision node. The hook returns the rejection reason to the model, which corrects it once; a second rejection rewrites the call to escalate. This keeps the graph acyclic. The plan described a retry edge; the behaviour (maximum one retry, then escalate) is the same.
- Human approval is a Strands interrupt raised in `ApprovalHook` before `submit_decision`. It propagates through the Graph (`Status.INTERRUPTED`) and the run resumes with an `interruptResponse`.
- Every block needs approval, and so does an allow above 5,000 AUD. Escalate never does, because it already hands the alert to an analyst.
- The Graph uses `FileSessionManager` per alert, and the run context is saved after each code node. A paused run can be approved from a new process: `tests/integration/test_pipeline.py::test_resume_after_restart_uses_session_files`.

## Consequences

- One `graph(...)` call per alert, plus one per approval.
- Actions run only in the action node, and a block without an approval record is downgraded to escalate there too (defence in depth).
- Starting an alert again deletes its old session and runs fresh; idempotent actions return the existing case.
