# Gatekeepers and guardrails: securing AI agents across your tool stack

Sample code for the AWS re:Invent 2026 code talk AIM331. A staff assistant for a fictional credit union calls tools through an [Amazon Bedrock AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/what-is-bedrock-agentcore.html) Gateway. The Gateway, two Lambda interceptors, a policy engine and a rate limit decide what the agent can do, for whom, and in what order.

> **Demo code, not for production.** The stack contains demo switches that turn controls off and on, so the talk can show each control before and after. All people, members, Social Security numbers and account numbers in the seed data are fictional. Deploy it only in an account you use for tests, and delete the stack when you finish.
>
> This is sample code, for non-production usage. You should work with your security and legal teams to meet your organizational security, regulatory and compliance requirements before deployment.

## What the sample shows

The agent, "Member Navigator", is a [Strands Agents](https://strandsagents.com/) agent on AgentCore Runtime. Two staff members use it: Maya Patel (member services) and Leo Martinez (lending). Dana Kim is the supervisor who approves large transfers. The talk has six moments; each one adds a control.

| Moment | Control | Code to read |
|---|---|---|
| 1 Front door | The Gateway accepts only a valid staff JWT from the allowed app clients. | `infra/gk_demo/stack.py` (`CfnGateway`, `authorizer_configuration`), `scripts/steps/m1_unauth.py` |
| 2 Per-person tokens | Each staff member connects the partner system once, on the managed consent portal (OAuth 2.0 authorization code flow through AgentCore Identity). Tokens stay per person in the token vault. Disconnecting one person leaves the other connected. | `infra/gk_demo/stack.py` (`PartnerProvider`, `PartnerTarget`, `ConsentPortal`), `lambdas/cr/handler.py`, `scripts/steps/m2_disconnect.py` |
| 3 Act on behalf | The REQUEST interceptor strips identity-like arguments that the model wrote, then signs a one-time, call-bound credential with AWS KMS: subject the staff member, actor the agent. The tool checks it and applies the staff member's own entitlements. | `lambdas/request_interceptor/handler.py`, `lambdas/tools/handler.py` |
| 4 Masking | The RESPONSE interceptor masks SSNs and full account numbers. A guardrail policy withholds member notes that contain an SSN. | `lambdas/response_interceptor/handler.py`, `policies/suppress_ssn_in_notes.policy` |
| 5a Sequence rule | A temporal policy forbids a transfer unless its destination is an account that the agent read earlier in the same session. | `policies/destination_seen_in_session.dw` |
| 5b Prompt guard | The RESPONSE interceptor checks partner referral text with Amazon Bedrock Guardrails (prompt-attack filter, `ApplyGuardrail`) and withholds it if the check flags it. It fails closed. | `lambdas/response_interceptor/handler.py`, `scripts/steps/m5c_guardrail.py` |
| 5c Human approval | A temporal policy forbids a transfer over 2,500 dollars unless a supervisor recorded a matching approval in the same session, through a separate approver app client. | `policies/approval_over_2500.dw`, `console/core.py` (approval) |
| 6 Rate limit | A Gateway rate limit per staff identity keeps one runaway session from starving everyone else. | `infra/gk_demo/stack.py` (`PerStaffLimit`), `scripts/steps/m6_limit.py`, `scripts/steps/m6_storm.py` |

The scripts and file names use build codes that differ in Moment 5: `M5` is Moment 5a, `M5c` is Moment 5b and `M5b` is Moment 5c (see `console/cards/README.md`).

## Architecture

```
Presenter laptop (127.0.0.1 only)
  Demo Console: FastAPI back end and one page (console/)
        |  HTTPS, the staff member's Cognito access token
        v
  AgentCore Runtime: Member Navigator (agent/, Strands, Claude Sonnet 4.6)
        |  MCP, the same token, one policy session per conversation
        v
  AgentCore Gateway: JWT authorizer, policy engine (ENFORCE), rate limit
        |-- REQUEST interceptor (Lambda): strips identity arguments, signs the act-on-behalf credential (KMS)
        |-- RESPONSE interceptor (Lambda): masking, prompt guard (Bedrock Guardrails), trail rows (DynamoDB)
        |
        |-- MemberRecords target: Lambda tools over DynamoDB (accounts, notes, loans, transfers, approvals)
        '-- Partner target: OpenAPI on an HTTP API with a JWT authorizer (partner Cognito pool),
            per-person OAuth tokens from AgentCore Identity, consent on the managed consent portal
```

One CDK stack (Python) holds everything: two Cognito user pools (staff and partner), the Gateway and its two targets, the policy engine and four policies, the interceptors, the Runtime, three DynamoDB tables, a KMS signing key, generated passwords in AWS Secrets Manager, and custom resources for the users and the consent portal.

## Prerequisites

- An AWS account for tests, and credentials that can deploy CloudFormation stacks that create IAM roles.
- Region `us-east-2` (the default), `us-east-1` or `us-west-2`: the three Regions behind the US cross-Region inference profile that the agent uses. The Region must offer the Amazon Bedrock AgentCore features this sample uses: Runtime, Gateway with interceptors and rate limits, Identity with the consent portal, and Policy.
- The account can invoke Anthropic Claude Sonnet 4.6 in Amazon Bedrock through the US cross-Region inference profile `us.anthropic.claude-sonnet-4-6`.
- The AWS CDK is bootstrapped in the account and Region (`cdk bootstrap aws://<account>/<region>`).
- On your computer: Linux or macOS with bash, Python 3.12, [uv](https://docs.astral.sh/uv/), Node.js 20 or later, the AWS CDK CLI 2.1143 or later, and the AWS CLI v2.

## Deploy

Run every command from the repository root.

1. Create the virtual environment and install the browser that the consent scripts use:

   ```
   uv venv --python 3.12 .venv
   uv pip install --python .venv/bin/python -r requirements.txt
   .venv/bin/playwright install chromium
   ```

2. Choose the stack name and Region, and tell the scripts which account the demo uses. `scripts/prep.py` and `scripts/preflight.py` refuse to run against any other account.

   ```
   export GK_STACK=gk-demo GK_REGION=us-east-2 GK_EXPECT_ACCOUNT=<your 12-digit account ID>
   ```

3. Show the changes, read the whole diff, then deploy. `scripts/deploy.sh` builds the two Lambda layers and the agent's code bundle first (Linux arm64 wheels, about 120 MB in total), and keeps the diff context for the deploy.

   ```
   bash scripts/deploy.sh diff first
   less logs/diff-first.log
   GK_DEPLOY_ARGS=--no-rollback bash scripts/deploy.sh deploy first
   ```

   The first create takes 2 to 4 minutes.

4. Pin the Gateway in the Gateway role. The role must exist before the Gateway, so the first deploy grants policy evaluation and the workload identity on every gateway in the account and Region (`gateway/*`). A second deploy narrows both to this gateway. (The consent portal role does not need this step: it names this gateway's workload identity from the first deploy.) Read the gateway ID:

   ```
   aws cloudformation describe-stacks --stack-name "$GK_STACK" --region "$GK_REGION" --query "Stacks[0].Outputs[?OutputKey=='GatewayId'].OutputValue" --output text
   ```

   Put it in `infra/cdk.json`, as `"stacks": {"gk-demo": {"gateway_id": "<GatewayId>"}}`, then:

   ```
   bash scripts/deploy.sh diff pin
   bash scripts/deploy.sh deploy pin
   ```

   Expect only two changes in the diff: the Gateway role's policy and the users' `SyncToken`.

5. Connect the two staff members to the partner system once, then put the stack in its start state and back:

   ```
   .venv/bin/python scripts/consent_login.py --all
   .venv/bin/python scripts/reset.py
   .venv/bin/python scripts/state.py on
   .venv/bin/python scripts/reset.py
   ```

A partner grant lasts 24 hours. `scripts/prep.py` and `scripts/preflight.py` renew Leo's grant when it is older than 12 hours. Maya connects live in Moment 1.

## Run the demo

Use two terminals, both in the repository root with the same AWS credentials and `GK_*` variables.

1. Terminal 1, the console:

   ```
   .venv/bin/python -m console
   ```

   Open http://127.0.0.1:8765/ and sign in as `presenter1`. The stack generated its password; read it from Secrets Manager (secret `gk-demo/staff/presenter1`, or `<stack>/staff/presenter1`).

2. Terminal 2, the step menu:

   ```
   ./run.sh
   ```

   The menu runs the pre-flight check first. Choose `1` for the full reset (about 3.5 minutes), restart the console, and press "New conversation" in both panes. Then follow the moments in order. Each step runs from the menu or from the console's Run buttons, and the console's step card shows the step's real code and its live output. The console's prompt picker holds the prompt for each moment.

`scripts/goto.py <M1|M2|M3|M4|M5|M5c|M5b|M6>` puts the stack in the start state of one moment, as if every earlier moment had run.

## If a step fails

- **Exit code 2, "ENGINE STOP".** A policy mode switch or the engine check failed with a policy engine error. Do not retry the step. Read the policy status with `.venv/bin/python scripts/state.py check`. If a policy stays in a failed state, delete the stack and deploy it again.
- **"no working AWS credentials" in the pre-flight or on the console.** Refresh the credentials in terminal 1, then restart the console.
- **Leo gets `CONSENT_REQUIRED`.** Run menu entry `5`, or `.venv/bin/python scripts/consent_login.py leo`.
- **Rate-limit throttles in Moment 3 after a Moment 6 run.** Stray throttles can continue for several minutes after the limit is raised again. Run the full reset (`scripts/prep.py`, menu entry `1`): it waits until the limit settles.
- **"NOT READY M6 storm (throttled)" right after the limit change.** The lower limit can take longer than the 35-second countdown to apply, especially on a new stack. Until it applies, the policy engine's own throttle answers first, and that throttle is not per identity, so it can reach Leo. Wait a minute, then run the storm again (menu entry `17`).
- **Maya's own prompt goes through during the Moment 6 storm.** The per-identity limit admits a share of calls each time its bucket refills, so a short agent turn can land between throttles. Leo's calls stay allowed either way, and the storm's counts (allowed and rate-limited per second) show the limit at work.

## Tests

Offline (no AWS, a few seconds):

```
.venv/bin/python -m pytest -q tests/test_console_direct.py tests/test_goto_plan.py tests/test_console_v3.py tests/test_preflight.py tests/test_stepcards.py tests/test_stepcard_console.py tests/test_rehearse_assertions.py tests/test_gk_mcp_classify.py
.venv/bin/python tools/code_truth.py console/cards/*.py
```

`tools/code_truth.py` checks that every code excerpt on a step card matches the source lines it cites.

Live (a deployed stack in the start state; they switch policy modes and rewrite the seed data, so run `scripts/state.py on` afterwards): `tests/test_policies_live.py`, `tests/test_prompt_guard_live.py`, `tests/test_agent_runtime_live.py`, `tests/test_console_live.py`. `scripts/rehearse.py` runs the whole talk with assertions, and `scripts/smoke_steps.sh` runs every step script once.

## Security notes

- **Console.** The console binds to `127.0.0.1` only, checks the `Host` and `Origin` headers, and signs in presenter accounts only. Never expose it through a tunnel, a proxy or a port forward. Step scripts reach it through a Unix socket in a mode-0700 directory.
- **Secrets.** The stack generates every password in Secrets Manager. No credential is in this repository, and the scripts never print a password or a token.
- **Signing key policy.** The act-on-behalf key has the AWS CDK default key policy, which gives the account root `kms:*`. The stack grants `kms:Sign` only to the REQUEST interceptor role and `kms:Verify` only to the tools role, but an IAM policy in the account can grant them to other principals too. In production, use a key policy that allows `kms:Sign` only for the signing role and `kms:Verify` only for the verifying role.
- **Demo switches.** SSM parameters (`/gk/deputy-mode`, `/gk/masking`, `/gk/prompt-guard`), policy modes (`LOG_ONLY` and `ACTIVE`) and the rate limit change during the talk so the audience sees each control before and after. In production, keep every control on and change it only through a reviewed deployment.
- **Prompt injection demo.** The agent's system prompt (`agent/main.py`) tells it to help staff act on requests in partner referrals, so the talk can show an indirect prompt injection and the controls that stop it. In production, do not tell an agent to follow instructions that arrive in tool output, and keep the prompt guard on.
- **Impersonation mode.** `scripts/steps/m3_mode.py impersonation` makes the REQUEST interceptor forward the staff member's raw token to the tool, which also puts the token in any Gateway application logs. It exists to show the anti-pattern on a slide. No script or test uses it. Do not use it.
- **Base permit.** The policy engine has one base permit for every authenticated caller on this gateway (`policies/permit_staff_tools.cedar`); forbid rules and the tools' entitlement checks narrow it. In production, write a permit per role and tool.
- **Approvals.** The ApprovalOver2500 policy (`policies/approval_over_2500.dw`) matches the member, the destination and the amount of an approval recorded earlier in the same policy session. It does not compare the staff member who starts the transfer with the requester of the approval. In production, bind each approval to its requester too, in the policy or in the tool, and keep each policy session to one user.
- **Policy validation.** The stack and the step scripts create and update policies with `validationMode` `IGNORE_ALL_FINDINGS`, so the demo can switch modes quickly. In production, use `FAIL_ON_ANY_FINDINGS` and fix each finding.
- **Error detail.** The Gateway sets `exceptionLevel` `DEBUG`, so callers see which control denied a call. Leave it unset in production.
- **Gateway application logs** are off by default (`-c gw_logs=on` turns them on). They carry full request and response bodies.
- **Rate limits** fail open on service timeouts and unresolvable dimensions. They are a cost and availability control, not an authorization boundary.
- **Fixed names.** Several resources have fixed names (`gk-...`), so deploy one copy per account and Region.

## Cost

Most resources bill per use: AgentCore Runtime, Gateway and Policy, Amazon Bedrock model invocations, Lambda, DynamoDB on-demand, API Gateway, Amazon Cognito and CloudWatch Logs. The stack also has fixed monthly costs for one KMS key and seven Secrets Manager secrets. See [AWS Pricing](https://aws.amazon.com/pricing/) for current prices, and delete the stack when you finish.

## Clean up

```
cd infra && cdk destroy "$GK_STACK" -c stack_name="$GK_STACK" -c region="$GK_REGION"
```

The KMS key goes into a 7-day pending deletion. Then list and delete the log groups that outlive the stack:

```
aws logs describe-log-groups --region "$GK_REGION" --log-group-name-prefix /aws/lambda/gk- --query "logGroups[].logGroupName"
aws logs describe-log-groups --region "$GK_REGION" --log-group-name-prefix "/aws/lambda/$GK_STACK-" --query "logGroups[].logGroupName"
aws logs describe-log-groups --region "$GK_REGION" --log-group-name-prefix /aws/bedrock-agentcore/runtimes/gk_member_navigator --query "logGroups[].logGroupName"
aws logs delete-log-group --region "$GK_REGION" --log-group-name <name>
```

Expect three: the Runtime's log group (`/aws/bedrock-agentcore/runtimes/gk_member_navigator-...`), the log group of the CDK custom-resource function (`/aws/lambda/<stack>-AWS679f53fac002430cb0da5b7982bd2287...`), and `/aws/lambda/gk-cr-framework`. The stack deletes `gk-cr-framework` itself, but the custom-resource framework function can log after that, and Lambda then creates the group again. Delete it before you deploy the stack again: the stack creates that log group by name, so a new deploy fails while it exists. Then delete the stack's assets in the CDK bootstrap bucket. If you deployed with `-c gw_logs=on`, delete the Gateway log group and its log delivery resources too: the stack retains them.

## Repository layout

| Path | What |
|---|---|
| `infra/` | The CDK app (Python): `app.py`, `gk_demo/stack.py`, the Runtime (`gk_demo/agent_runtime.py`), tool schemas, policy generations |
| `lambdas/` | The tools, the two interceptors, the partner referral API, the seed and reset function, the custom resources |
| `policies/` | The policies in readable form (Cedar, temporal and guardrail policies) |
| `agent/` | Member Navigator, the Strands agent on AgentCore Runtime |
| `console/` | The Demo Console and its step cards |
| `scripts/` | Deploy, reset, the step scripts in `scripts/steps/`, the pre-flight, `goto.py`, the rehearsal harness |
| `layers/` | The pinned requirements of the two Lambda layers (built into `layers/*/python/`, git-ignored) |
| `tests/` | Offline and live tests |
| `tools/` | `code_truth.py`, the step-card excerpt check |

## Resources

- [Amazon Bedrock AgentCore Developer Guide](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/what-is-bedrock-agentcore.html)
- [Amazon Bedrock Guardrails](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails.html)
- [Cedar policy language](https://docs.cedarpolicy.com/)
- [Strands Agents](https://strandsagents.com/)

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.

Although this repository is released under the MIT-0 license, its Python dependencies (`requirements.txt` and `agent/requirements.txt`) use the third party [certifi](https://github.com/certifi/python-certifi) project. The certifi project's licensing includes the MPL-2.0 license.
