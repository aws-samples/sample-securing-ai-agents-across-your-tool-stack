# policies/

The demo's AgentCore Policy statements in readable form, with comments. A statement is the file without its `//` comment lines. `.cedar` files go in `Definition.Cedar`; `.dw` (temporal) and `.policy` (guardrail) files go in `Definition.Policy`.

| File | Kind | What it does | Moment |
|---|---|---|---|
| `permit_staff_tools.cedar` | Cedar permit | The base permit: an authenticated caller may call the gateway's tools. Cedar denies by default. | all |
| `suppress_ssn_in_notes.policy` | guardrail | Withholds a notes result when the SensitiveInformation check finds an SSN. | 4 |
| `destination_seen_in_session.dw` | temporal | Forbids a transfer unless its destination was returned as the member's own checking or savings account earlier in the same session. | 5a |
| `approval_over_2500.dw` | temporal | Forbids a transfer over 2,500 dollars unless a matching supervisor approval was recorded earlier in the same session. | 5c |
| `prompt_attack_on_referrals.policy` | guardrail | Withholds partner referrals that the prompt-attack check flags. Not deployed by the demo, which runs this check in the RESPONSE interceptor instead (Moment 5b). | none |
| `read_burst_per_session.dw` | temporal | Forbids an account read once the policy session made 20 of them in the last minute. Not deployed by the demo; `scripts/steps/m6_count.py` uses it only on an engine that has it. | none |

What the stack deploys: generation 2 (`infra/gk_demo/generations.py`), the four rules of moments 4, 5a and 5c and the base permit, with the statement text in `infra/gk_demo/policy_sources.py` scoped to the demo's gateway ARN. The files here have the scope `resource is AgentCore::Gateway`; generation 3 loads them and replaces that scope with this stack's gateway.

Write a temporal rule with two conditions as two `unless temporal` clauses, which Cedar joins with AND, and keep a guardrail predicate on one line. The stack and the step scripts create and update policies with `validationMode IGNORE_ALL_FINDINGS` so the demo can switch modes quickly; use `FAIL_ON_ANY_FINDINGS` in production.
