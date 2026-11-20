"""The agent on AgentCore Runtime: L2 `Runtime` with direct code deployment.

The code bundle is built by scripts/build_agent.sh (Linux arm64 wheels for Python 3.12 plus the agent sources) into
agent/.build/pkg-<hash>; agent/.build/CURRENT names it. scripts/deploy.sh runs the build before every diff and deploy.
Inbound auth is the staff pool's JWT (the `console` client only); the Authorization header is on the allowlist so the
agent can forward the same token to the Gateway.

Execution role `<stack>-agent-runtime`: written here in full and handed to the L2 as an immutable role, so the L2's
broad default grants (read on the whole CDK asset bucket) are not added. It carries the service's runtime needs (its
logs, X-Ray, metrics, workload access tokens), model invocation on the Sonnet 4.6 US inference profile and its
foundation model in the three Regions the profile routes to, and read on the one code object. No Gateway permission:
the Gateway authorizes the forwarded staff JWT, not IAM. No DynamoDB, Secrets Manager, SSM or KMS.
"""
import os

from aws_cdk import CfnOutput, Duration, aws_bedrockagentcore as ac, aws_iam as iam, aws_s3 as s3, aws_s3_assets as s3a

AGENT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "agent"))
RUNTIME_NAME = "gk_member_navigator"
MODEL_PROFILE = "us.anthropic.claude-sonnet-4-6"
MODEL_FOUNDATION = "anthropic.claude-sonnet-4-6"
PROFILE_REGIONS = ("us-east-1", "us-east-2", "us-west-2")


def bundle_dir():
    marker = os.path.join(AGENT, ".build", "CURRENT")
    if not os.path.exists(marker):
        raise RuntimeError("agent bundle missing: run `bash scripts/build_agent.sh` (scripts/deploy.sh does it)")
    path = os.path.join(AGENT, ".build", open(marker).read().strip())
    if not os.path.isdir(path):
        raise RuntimeError(f"agent bundle {path} not found: run `bash scripts/build_agent.sh`")
    return path


def add_agent_runtime(stack, *, discovery_url, console_client_id, gateway_url):
    region, acct, part = stack.region, stack.account, stack.partition
    code = s3a.Asset(stack, "AgentCode", path=bundle_dir())
    logs_arn = f"arn:{part}:logs:{region}:{acct}:log-group:/aws/bedrock-agentcore/runtimes/*"
    wid = f"arn:{part}:bedrock-agentcore:{region}:{acct}:workload-identity-directory/default"
    doc = iam.PolicyDocument(statements=[
        iam.PolicyStatement(sid="RuntimeLogGroups", actions=["logs:CreateLogGroup", "logs:DescribeLogStreams"], resources=[logs_arn]),
        iam.PolicyStatement(sid="DescribeLogGroups", actions=["logs:DescribeLogGroups"], resources=[f"arn:{part}:logs:{region}:{acct}:log-group:*"]),
        iam.PolicyStatement(sid="RuntimeLogStreams", actions=["logs:CreateLogStream", "logs:PutLogEvents"], resources=[f"{logs_arn}:log-stream:*"]),
        iam.PolicyStatement(sid="XRay", actions=["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules",
                                                 "xray:GetSamplingTargets"], resources=["*"]),
        iam.PolicyStatement(sid="Metrics", actions=["cloudwatch:PutMetricData"], resources=["*"],
                            conditions={"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}}),
        # The service mints the runtime's workload access token for the runtime's own workload identity.
        iam.PolicyStatement(sid="WorkloadAccessToken", actions=["bedrock-agentcore:GetWorkloadAccessToken", "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                                                                "bedrock-agentcore:GetWorkloadAccessTokenForUserId"],
                            resources=[wid, f"{wid}/workload-identity/*"]),
        iam.PolicyStatement(sid="InvokeModel", actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                            resources=[f"arn:{part}:bedrock:{region}:{acct}:inference-profile/{MODEL_PROFILE}"]
                                      + [f"arn:{part}:bedrock:{r}::foundation-model/{MODEL_FOUNDATION}" for r in PROFILE_REGIONS]),
        iam.PolicyStatement(sid="CodeObject", actions=["s3:GetObject"], resources=[f"arn:{part}:s3:::{code.s3_bucket_name}/{code.s3_object_key}"]),
    ])
    role = iam.Role(stack, "AgentRuntimeRole", role_name=f"{stack.stack_name}-agent-runtime", description="gk agent on AgentCore Runtime",
                    assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com", conditions={
                        "StringEquals": {"aws:SourceAccount": acct},
                        "ArnLike": {"aws:SourceArn": f"arn:{part}:bedrock-agentcore:{region}:{acct}:runtime/{RUNTIME_NAME}*"}}),
                    inline_policies={"agent-runtime": doc}, max_session_duration=Duration.hours(8))
    runtime = ac.Runtime(
        stack, "AgentRuntime", runtime_name=RUNTIME_NAME,
        description="Gatekeepers demo agent (Strands, Claude Sonnet 4.6) behind the gk gateway",
        agent_runtime_artifact=ac.AgentRuntimeArtifact.from_s3(s3.Location(bucket_name=code.s3_bucket_name, object_key=code.s3_object_key),
                                                               ac.AgentCoreRuntime.PYTHON_3_12, ["main.py"]),
        # Immutable: the L2's own grants on this role become no-ops, so the role is exactly the document above.
        execution_role=iam.Role.from_role_arn(stack, "AgentRuntimeRoleRef", role.role_arn, mutable=False),
        authorizer_configuration=ac.RuntimeAuthorizerConfiguration.using_jwt(discovery_url, allowed_clients=[console_client_id]),
        request_header_configuration=ac.RequestHeaderConfiguration(allowlisted_headers=["Authorization"]),
        lifecycle_configuration=ac.LifecycleConfiguration(idle_runtime_session_timeout=Duration.seconds(7200), max_lifetime=Duration.seconds(28800)),
        environment_variables={"GK_GATEWAY_URL": gateway_url, "GK_MODEL_ID": MODEL_PROFILE, "GK_AGENT_BIND": "0.0.0.0",  # nosec B104 - inside the Runtime microVM
                               "PYTHONUNBUFFERED": "1"})
    runtime.node.add_dependency(role)
    CfnOutput(stack, "AgentRuntimeArn", value=runtime.agent_runtime_arn)
    CfnOutput(stack, "AgentRuntimeId", value=runtime.agent_runtime_id)
    return runtime
