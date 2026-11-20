"""The demo stack: every resource the talk uses, in one CloudFormation stack.

Everything is gk- prefixed; the project tag is applied in app.py. Several names are fixed (tables, functions, pools,
SSM parameters), so deploy one copy per account and Region. AgentCore resources are L1 (CfnX) so the template is
explicit; the consent portal is a custom resource. Creation order follows the consent portal docs: gateway (no 3LO
target) -> portal -> callback registrations -> partner (3LO) target.
"""
import os

from aws_cdk import (CfnOutput, CustomResource, Duration, Fn, RemovalPolicy, Stack, aws_apigatewayv2 as apigw,
                     aws_apigatewayv2_authorizers as apigw_auth, aws_apigatewayv2_integrations as apigw_int,
                     aws_bedrock as bedrock, aws_bedrockagentcore as ac, aws_cognito as cognito, aws_dynamodb as ddb, aws_iam as iam,
                     aws_kms as kms, aws_lambda as lambda_, aws_logs as logs, aws_secretsmanager as sm, aws_ssm as ssm,
                     custom_resources as cr)
from constructs import Construct

from gk_demo.agent_runtime import add_agent_runtime
from gk_demo.generations import GENERATIONS, load_statement
from gk_demo.policy_sources import act as _act, demo_statements
from gk_demo.tool_schema import GT, tools

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
LAMBDAS = os.path.join(ROOT, "lambdas")
LAYERS = os.path.join(ROOT, "layers")
PRV = ac.CfnOAuth2CredentialProvider


class GkDemoStack(Stack):
    def __init__(self, scope: Construct, cid: str, **kw):
        super().__init__(scope, cid, **kw)
        sfx = self.node.try_get_context("sfx")  # Cognito domain suffix, unique per Region across all accounts (app.py)
        if not sfx:
            raise ValueError("context value sfx is missing: app.py sets it, or pass -c sfx=<suffix>")
        acct, region = self.account, self.region

        # ---------------- staff pool (demo identities) ----------------
        # Demo identities only. The stack generates every password (20 characters, every character type, Secrets Manager).
        pw_policy = cognito.PasswordPolicy(min_length=12, require_lowercase=True, require_uppercase=True, require_digits=True,
                                           require_symbols=True)
        staff = cognito.UserPool(self, "StaffPool", user_pool_name="gk-staff", self_sign_up_enabled=False, password_policy=pw_policy,
                                 sign_in_aliases=cognito.SignInAliases(username=True), removal_policy=RemovalPolicy.DESTROY)
        staff_domain = staff.add_domain("StaffDomain", cognito_domain=cognito.CognitoDomainOptions(domain_prefix=f"gk-staff-{sfx}"))
        # presenter: console sign-in only; the BFF checks the group and keeps no presenter token
        for g in ("member_services", "lending", "supervisor", "presenter"):
            cognito.CfnUserPoolGroup(self, f"Group{g}", user_pool_id=staff.user_pool_id, group_name=g)
        console = staff.add_client("ConsoleClient", user_pool_client_name="gk-console", generate_secret=False, disable_o_auth=True,
                                   auth_flows=cognito.AuthFlow(admin_user_password=True), access_token_validity=Duration.minutes(60),
                                   id_token_validity=Duration.minutes(60), refresh_token_validity=Duration.days(1))
        approver = staff.add_client("ApproverClient", user_pool_client_name="gk-approver", generate_secret=False, disable_o_auth=True,
                                    auth_flows=cognito.AuthFlow(admin_user_password=True), access_token_validity=Duration.minutes(5),
                                    id_token_validity=Duration.minutes(5), refresh_token_validity=Duration.minutes(60))
        portal_login = staff.add_client("PortalLoginClient", user_pool_client_name="gk-portal-login", generate_secret=True,
                                        supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO],
                                        o_auth=cognito.OAuthSettings(flows=cognito.OAuthFlows(authorization_code_grant=True),
                                                                     scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
                                                                     callback_urls=["https://example.com/placeholder"]))
        staff_iss = f"https://cognito-idp.{region}.amazonaws.com/{staff.user_pool_id}"
        staff_disc = f"{staff_iss}/.well-known/openid-configuration"

        # ---------------- partner pool (self-contained partner) ----------------
        partner = cognito.UserPool(self, "PartnerPool", user_pool_name="gk-partner", self_sign_up_enabled=False, password_policy=pw_policy,
                                   sign_in_aliases=cognito.SignInAliases(username=True), removal_policy=RemovalPolicy.DESTROY)
        partner_domain = partner.add_domain("PartnerDomain", cognito_domain=cognito.CognitoDomainOptions(domain_prefix=f"gk-partner-{sfx}"))
        read_scope = cognito.ResourceServerScope(scope_name="read", scope_description="Read your own referrals")
        rs = partner.add_resource_server("ReferralsResourceServer", identifier="referrals", user_pool_resource_server_name="gk-referrals", scopes=[read_scope])
        partner_client = partner.add_client("PartnerAgentCoreClient", user_pool_client_name="gk-partner-agentcore", generate_secret=True,
                                            supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO],
                                            o_auth=cognito.OAuthSettings(flows=cognito.OAuthFlows(authorization_code_grant=True),
                                                                         scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.resource_server(rs, read_scope)],
                                                                         callback_urls=["https://example.com/placeholder"]),
                                            access_token_validity=Duration.minutes(60), refresh_token_validity=Duration.days(1))
        partner_iss = f"https://cognito-idp.{region}.amazonaws.com/{partner.user_pool_id}"

        # ---------------- secrets (generated passwords, never in properties) ----------------
        gen = sm.SecretStringGenerator(password_length=20, exclude_characters="\"'\\/@ `", require_each_included_type=True)
        secrets = {}
        for pool_name, user in (("staff", "maya"), ("staff", "leo"), ("staff", "dana"), ("staff", "presenter1"), ("staff", "presenter2"),
                                ("partner", "maya"), ("partner", "leo")):
            secrets[(pool_name, user)] = sm.Secret(self, f"Pw{pool_name}{user}", secret_name=f"{self.stack_name}/{pool_name}/{user}",
                                                   generate_secret_string=gen, removal_policy=RemovalPolicy.DESTROY)

        # ---------------- data ----------------
        def table(cid, name, ttl=None):
            return ddb.Table(self, cid, table_name=name, partition_key=ddb.Attribute(name="pk", type=ddb.AttributeType.STRING),
                             sort_key=ddb.Attribute(name="sk", type=ddb.AttributeType.STRING), billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
                             time_to_live_attribute=ttl, removal_policy=RemovalPolicy.DESTROY)
        data_t = table("DataTable", "gk-data")
        events_t = table("EventsTable", "gk-events", ttl="ttl")
        refs_t = table("ReferralsTable", "gk-partner-referrals")

        key = kms.Key(self, "SigningKey", key_spec=kms.KeySpec.ECC_NIST_P256, key_usage=kms.KeyUsage.SIGN_VERIFY,
                      alias=f"alias/{self.stack_name}-act-on-behalf", removal_policy=RemovalPolicy.DESTROY, pending_window=Duration.days(7))
        mode_p = ssm.StringParameter(self, "DeputyMode", parameter_name="/gk/deputy-mode", string_value="service")
        mask_p = ssm.StringParameter(self, "Masking", parameter_name="/gk/masking", string_value="off")
        portal_p = ssm.StringParameter(self, "PortalUrlParam", parameter_name="/gk/portal-url", string_value="pending")

        boto3_layer = lambda_.LayerVersion(self, "Boto3Layer", layer_version_name="gk-boto3", code=lambda_.Code.from_asset(os.path.join(LAYERS, "boto3")),
                                           compatible_runtimes=[lambda_.Runtime.PYTHON_3_12], compatible_architectures=[lambda_.Architecture.ARM_64])
        jwt_layer = lambda_.LayerVersion(self, "JwtLayer", layer_version_name="gk-pyjwt", code=lambda_.Code.from_asset(os.path.join(LAYERS, "pyjwt")),
                                         compatible_runtimes=[lambda_.Runtime.PYTHON_3_12], compatible_architectures=[lambda_.Architecture.ARM_64])

        def fn(cid, name, folder, env, layers=(), timeout=30):
            lg = logs.LogGroup(self, f"{cid}Logs", log_group_name=f"/aws/lambda/{name}", retention=logs.RetentionDays.ONE_WEEK,
                               removal_policy=RemovalPolicy.DESTROY)
            return lambda_.Function(self, cid, function_name=name, runtime=lambda_.Runtime.PYTHON_3_12, architecture=lambda_.Architecture.ARM_64,
                                    handler="handler.handler", code=lambda_.Code.from_asset(os.path.join(LAMBDAS, folder)), environment=env,
                                    layers=list(layers), timeout=Duration.seconds(timeout), memory_size=512, log_group=lg)

        tools_fn = fn("ToolsFn", "gk-member-records", "tools",
                      {"DATA_TABLE": data_t.table_name, "EVENTS_TABLE": events_t.table_name, "SIGNING_KEY_ID": key.key_id,
                       "MODE_PARAM": mode_p.parameter_name, "STAFF_ISSUER": staff_iss, "CONSOLE_CLIENT_ID": console.user_pool_client_id,
                       "APPROVER_CLIENT_ID": approver.user_pool_client_id, "LOG_SHAPE": "1"}, [jwt_layer])
        data_t.grant_read_data(tools_fn)
        # The tools only write to gk-events: replay markers (conditional PutItem) and transfer records.
        tools_fn.add_to_role_policy(iam.PolicyStatement(actions=["dynamodb:PutItem"], resources=[events_t.table_arn]))
        mode_p.grant_read(tools_fn)
        tools_fn.add_to_role_policy(iam.PolicyStatement(actions=["kms:Verify"], resources=[key.key_arn]))

        req_fn = fn("RequestInterceptorFn", "gk-request-interceptor", "request_interceptor",
                    {"SIGNING_KEY_ID": key.key_id, "MODE_PARAM": mode_p.parameter_name})
        mode_p.grant_read(req_fn)
        req_fn.add_to_role_policy(iam.PolicyStatement(actions=["kms:Sign"], resources=[key.key_arn],
                                                      conditions={"StringEquals": {"kms:SigningAlgorithm": "ECDSA_SHA_256"}}))
        # M5c prompt guard: the demo keeps the prompt-attack check out of the policy engine. The RESPONSE
        # interceptor runs this Bedrock guardrail (prompt-attack filter) over partner referral text with ApplyGuardrail.
        # /gk/prompt-guard gates it: the template's end state is `on`; reset turns it `off`; the M5c step turns it `on`.
        guard_p = ssm.StringParameter(self, "PromptGuardParam", parameter_name="/gk/prompt-guard", string_value="on")
        prompt_guard = bedrock.CfnGuardrail(
            self, "PromptGuard", name="gk-prompt-guard", description="gk demo: prompt-attack check on partner referral text (M5c)",
            blocked_input_messaging="Blocked: this partner content looks like a prompt attack.",
            blocked_outputs_messaging="Blocked by the gk prompt guard.",
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(filters_config=[
                bedrock.CfnGuardrail.ContentFilterConfigProperty(type="PROMPT_ATTACK", input_strength="HIGH", output_strength="NONE")]))
        resp_fn = fn("ResponseInterceptorFn", "gk-response-interceptor", "response_interceptor",
                     {"MASK_PARAM": mask_p.parameter_name, "PORTAL_PARAM": portal_p.parameter_name, "GUARD_PARAM": guard_p.parameter_name,
                      "GUARDRAIL_ID": prompt_guard.attr_guardrail_id, "GUARDRAIL_VERSION": "DRAFT", "EVENTS_TABLE": events_t.table_name})
        mask_p.grant_read(resp_fn)
        portal_p.grant_read(resp_fn)
        guard_p.grant_read(resp_fn)
        resp_fn.add_to_role_policy(iam.PolicyStatement(actions=["bedrock:ApplyGuardrail"], resources=[prompt_guard.attr_guardrail_arn]))
        resp_fn.add_to_role_policy(iam.PolicyStatement(actions=["dynamodb:PutItem"], resources=[events_t.table_arn]))  # trail rows
        CfnOutput(self, "PromptGuardId", value=prompt_guard.attr_guardrail_id)

        refs_fn = fn("PartnerReferralsFn", "gk-partner-referrals", "partner_referrals", {"REFERRALS_TABLE": refs_t.table_name})
        refs_t.grant_read_data(refs_fn)

        # ---------------- partner referral API (HTTP API + JWT authorizer on the partner pool) ----------------
        api = apigw.HttpApi(self, "PartnerApi", api_name="gk-partner-api")
        jwt_auth = apigw_auth.HttpJwtAuthorizer("PartnerJwt", jwt_issuer=partner_iss, jwt_audience=[partner_client.user_pool_client_id],
                                                authorizer_name="gk-partner-jwt")  # an authorizer name, not a key  # nosemgrep
        api.add_routes(path="/referrals", methods=[apigw.HttpMethod.GET], integration=apigw_int.HttpLambdaIntegration("ReferralsInt", refs_fn),
                       authorizer=jwt_auth, authorization_scopes=["referrals/read"])

        # ---------------- custom resource provider (users, portal, callbacks) ----------------
        cr_fn = fn("CrFn", "gk-cr", "cr", {}, [boto3_layer], timeout=900)
        cr_fn.add_to_role_policy(iam.PolicyStatement(actions=["cognito-idp:AdminCreateUser", "cognito-idp:AdminSetUserPassword", "cognito-idp:AdminAddUserToGroup",
                                                              "cognito-idp:DescribeUserPoolClient", "cognito-idp:UpdateUserPoolClient"],
                                                     resources=[staff.user_pool_arn, partner.user_pool_arn]))
        for s in secrets.values():
            s.grant_read(cr_fn)
        # Consent portal permissions for the portal custom resource: below, next to the portal role, scoped to resources.
        portal_p.grant_write(cr_fn)
        provider = cr.Provider(self, "CrProvider", on_event_handler=cr_fn, provider_function_name="gk-cr-framework",
                               log_group=logs.LogGroup(self, "CrFrameworkLogs", log_group_name="/aws/lambda/gk-cr-framework",
                                                       retention=logs.RetentionDays.ONE_WEEK, removal_policy=RemovalPolicy.DESTROY))

        # users_sync (context): scripts/deploy.sh passes a new value on every deploy, so this custom resource runs its Update
        # and re-sets every Cognito password from Secrets Manager. Without the context value the
        # properties do not change, so a plain `cdk diff` stays clean.
        users_sync = self.node.try_get_context("users_sync")

        def users_cr(cid, name, pool, specs):
            props = {"Kind": "users", "Name": name, "Users": [
                {"PoolId": pool.user_pool_id, "Username": u, "SecretArn": secrets[(name, u)].secret_arn, "Groups": g}
                for u, g in specs]}
            if users_sync:
                props["SyncToken"] = str(users_sync)
            return CustomResource(self, cid, service_token=provider.service_token, resource_type="Custom::GkUsers", properties=props)
        staff_users = users_cr("StaffUsers", "staff", staff, [("maya", ["member_services"]), ("leo", ["lending"]), ("dana", ["supervisor"]),
                                                             ("presenter1", ["presenter"]), ("presenter2", ["presenter"])])
        staff_users.node.add_dependency(*[c for c in self.node.children if isinstance(c, cognito.CfnUserPoolGroup)])
        users_cr("PartnerUsers", "partner", partner, [("maya", []), ("leo", [])])

        # ---------------- seed and reset function ----------------
        reset_fn = fn("ResetFn", "gk-reset", "reset",
                      {"DATA_TABLE": data_t.table_name, "REFERRALS_TABLE": refs_t.table_name, "MODE_PARAM": mode_p.parameter_name,
                       "MASK_PARAM": mask_p.parameter_name, "STAFF_POOL_ID": staff.user_pool_id, "CONSOLE_CLIENT_ID": console.user_pool_client_id,
                       "STAFF_SECRETS": self.to_json_string({u: secrets[("staff", u)].secret_arn for u in ("maya", "leo", "dana")}),
                       "PARTNER_TOOL": "Partner___partner_list_referrals"}, [boto3_layer], timeout=120)
        data_t.grant_read_write_data(reset_fn)
        refs_t.grant_read_write_data(reset_fn)
        mode_p.grant_write(reset_fn)
        mask_p.grant_write(reset_fn)
        for u in ("maya", "leo", "dana"):
            secrets[("staff", u)].grant_read(reset_fn)
        reset_fn.add_to_role_policy(iam.PolicyStatement(actions=["cognito-idp:AdminInitiateAuth"], resources=[staff.user_pool_arn]))
        seed_provider = cr.Provider(self, "SeedProvider", on_event_handler=reset_fn, provider_function_name="gk-seed-framework",
                                    log_group=logs.LogGroup(self, "SeedFrameworkLogs", log_group_name="/aws/lambda/gk-seed-framework",
                                                            retention=logs.RetentionDays.ONE_WEEK, removal_policy=RemovalPolicy.DESTROY))
        CustomResource(self, "Seed", service_token=seed_provider.service_token, resource_type="Custom::GkSeed", properties={"Version": "1"})

        # ---------------- AgentCore Identity: credential providers ----------------
        staff_idp = PRV(self, "StaffPortalIdp", name="gk-staff-portal-idp", credential_provider_vendor="CustomOauth2",
                        oauth2_provider_config_input=PRV.Oauth2ProviderConfigInputProperty(custom_oauth2_provider_config=PRV.CustomOauth2ProviderConfigInputProperty(
                            oauth_discovery=PRV.Oauth2DiscoveryProperty(discovery_url=staff_disc), client_id=portal_login.user_pool_client_id,
                            client_secret=portal_login.user_pool_client_secret.unsafe_unwrap())))
        partner_prov = PRV(self, "PartnerProvider", name="gk-counseling-partner", credential_provider_vendor="CustomOauth2",
                           oauth2_provider_config_input=PRV.Oauth2ProviderConfigInputProperty(custom_oauth2_provider_config=PRV.CustomOauth2ProviderConfigInputProperty(
                               oauth_discovery=PRV.Oauth2DiscoveryProperty(discovery_url=f"{partner_iss}/.well-known/openid-configuration"),
                               client_id=partner_client.user_pool_client_id, client_secret=partner_client.user_pool_client_secret.unsafe_unwrap())))

        # ---------------- policy engine and gateway ----------------
        # Policy engine generations (gk_demo/generations.py). -c policy_gens=2,3 lists the generations in the
        # template; -c policy_gen=N is the one the Gateway uses. Inactive generations are retained (RETAIN), so dropping one
        # from policy_gens orphans it without an API call. A bump takes two deploys: add the generation (Gateway unchanged,
        # verify its policies are ACTIVE), then move policy_gen (the Gateway points at the new engine).
        gens = [int(g) for g in str(self.node.try_get_context("policy_gens") or "2").split(",")]
        active_gen = int(self.node.try_get_context("policy_gen") or 2)
        if active_gen not in gens:
            raise ValueError(f"policy_gen {active_gen} is not in policy_gens {gens}")
        engines = {}
        for g in gens:
            spec = GENERATIONS[g]
            engines[g] = ac.CfnPolicyEngine(self, spec.engine_cid, name=spec.engine_name, description=spec.engine_description)
            if g != active_gen:
                engines[g].apply_removal_policy(RemovalPolicy.RETAIN)
        engine_active = engines[active_gen]
        # Trust: this account, and as aws:SourceArn an AgentCore resource in this account and Region. This is the trust policy
        # that the AgentCore Policy permissions guide gives the execution role of a gateway with a policy engine.
        gw_role = iam.Role(self, "GatewayRole", role_name=f"{self.stack_name}-gateway-role",
                           assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com", conditions={
                               "StringEquals": {"aws:SourceAccount": acct},
                               "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{region}:{acct}:*"}}))
        gw_role.add_to_policy(iam.PolicyStatement(actions=["lambda:InvokeFunction"], resources=[tools_fn.function_arn, req_fn.function_arn, resp_fn.function_arn]))
        # Least privilege. The actions are the ones the Gateway calls with this role: CloudTrail
        # shows GetWorkloadAccessToken, GetWorkloadAccessTokenForJWT and GetResourceOauth2Token on its own workload identity
        # and the partner provider, and GetSecretValue on the provider's managed secret. The workload
        # identity is named after the gateway ID, so it is pinned when cdk.json knows the ID; on a fresh install the ID is not
        # known before the Gateway exists (and the Gateway depends on this role), so the last segment is a wildcard.
        gw_id_known = ((self.node.try_get_context("stacks") or {}).get(self.stack_name) or {}).get("gateway_id")
        wid_dir = f"arn:aws:bedrock-agentcore:{region}:{acct}:workload-identity-directory/default"
        gw_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock-agentcore:GetWorkloadAccessToken", "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                     "bedrock-agentcore:GetResourceOauth2Token"],
            resources=[wid_dir, f"{wid_dir}/workload-identity/{gw_id_known or '*'}",
                       f"arn:aws:bedrock-agentcore:{region}:{acct}:token-vault/default", partner_prov.attr_credential_provider_arn]))
        # Policy evaluation (docs, policy-permissions.html): AuthorizeAction and PartiallyAuthorizeActions need BOTH the policy
        # engine and the gateway as resources; GetPolicyEngine needs only the engine. Every generation's engine is listed, so
        # a generation switch keeps working. The gateway ARN cannot be a GetAtt (the Gateway depends on this role), so it is
        # the literal ARN from cdk.json, or gateway/* on a fresh install. With the engines only, every tool call
        # fails with -32002 "Insufficient Permissions for Policy Evaluation".
        engine_arns = [e.attr_policy_engine_arn for e in engines.values()]
        gw_arn_known = f"arn:aws:bedrock-agentcore:{region}:{acct}:gateway/{gw_id_known or '*'}"
        gw_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock-agentcore:AuthorizeAction", "bedrock-agentcore:PartiallyAuthorizeActions"], resources=[*engine_arns, gw_arn_known]))
        gw_role.add_to_policy(iam.PolicyStatement(actions=["bedrock-agentcore:GetPolicyEngine"], resources=engine_arns))
        # Guardrail policies (SuppressSsnInNotes). Wildcard on purpose: the AgentCore Policy guardrail docs grant this action on
        # "*" and name no resource type for it. bedrock:ApplyGuardrail is not here: only the response interceptor calls it,
        # with its own role, on its own guardrail.
        gw_role.add_to_policy(iam.PolicyStatement(actions=["bedrock:InvokeGuardrailChecks"], resources=["*"]))
        gw_role.add_to_policy(iam.PolicyStatement(actions=["secretsmanager:GetSecretValue"],
                                                  resources=[f"arn:aws:secretsmanager:{region}:{acct}:secret:bedrock-agentcore-identity!default/oauth2/gk-counseling-partner-*"]))
        GWP = ac.CfnGateway
        gw = GWP(self, "Gateway", name=f"{self.stack_name}-gw", role_arn=gw_role.role_arn, protocol_type="MCP", authorizer_type="CUSTOM_JWT",
                 description="gk demo gateway", exception_level="DEBUG",
                 protocol_configuration=GWP.GatewayProtocolConfigurationProperty(mcp=GWP.MCPGatewayConfigurationProperty(
                     supported_versions=["2025-11-25", "2025-06-18"], session_configuration=GWP.SessionConfigurationProperty(session_timeout_in_seconds=7200))),
                 authorizer_configuration=GWP.AuthorizerConfigurationProperty(custom_jwt_authorizer=GWP.CustomJWTAuthorizerConfigurationProperty(
                     discovery_url=staff_disc, allowed_clients=[console.user_pool_client_id, portal_login.user_pool_client_id])),
                 interceptor_configurations=[
                     GWP.GatewayInterceptorConfigurationProperty(interceptor=GWP.InterceptorConfigurationProperty(lambda_=GWP.LambdaInterceptorConfigurationProperty(arn=req_fn.function_arn)),
                                                                 interception_points=["REQUEST"], input_configuration=GWP.InterceptorInputConfigurationProperty(pass_request_headers=True)),
                     GWP.GatewayInterceptorConfigurationProperty(interceptor=GWP.InterceptorConfigurationProperty(lambda_=GWP.LambdaInterceptorConfigurationProperty(arn=resp_fn.function_arn)),
                                                                 interception_points=["RESPONSE"], input_configuration=GWP.InterceptorInputConfigurationProperty(pass_request_headers=True))],
                 # The Gateway resource owns its policy engine. Any Gateway update re-applies every policy whose
                 # statement references GetAtt Gateway.GatewayArn.
                 policy_engine_configuration=GWP.GatewayPolicyEngineConfigurationProperty(arn=engine_active.attr_policy_engine_arn, mode="ENFORCE"))
        gw.node.add_dependency(gw_role)
        reset_fn.add_environment("GATEWAY_URL", gw.attr_gateway_url)

        # ---------------- agent on AgentCore Runtime (gk_demo/agent_runtime.py) ----------------
        add_agent_runtime(self, discovery_url=staff_disc, console_client_id=console.user_pool_client_id, gateway_url=gw.attr_gateway_url)

        member_target = GT(self, "MemberRecordsTarget", name="MemberRecords", gateway_identifier=gw.attr_gateway_identifier,
                           description="Internal member records (Lambda)",
                           target_configuration=GT.TargetConfigurationProperty(mcp=GT.McpTargetConfigurationProperty(lambda_=GT.McpLambdaTargetConfigurationProperty(
                               lambda_arn=tools_fn.function_arn, tool_schema=GT.ToolSchemaProperty(inline_payload=tools())))),
                           credential_provider_configurations=[GT.CredentialProviderConfigurationProperty(credential_provider_type="GATEWAY_IAM_ROLE")])

        # ---------------- consent portal (custom resource) and callback registrations ----------------
        # The consent portal's execution role, as in the AgentCore Identity guide ("Consent portal execution role"): the
        # trust policy pins the account and, as the guide recommends, this stack's portal (its ID starts with its name).
        portal_role = iam.Role(self, "PortalRole", role_name=f"{self.stack_name}-portal-role",
                               assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com", conditions={
                                   "StringEquals": {"aws:SourceAccount": acct},
                                   "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{region}:{acct}:consent-portal/{self.stack_name}-portal-*"}}))
        portal_role.add_to_policy(iam.PolicyStatement(actions=["bedrock-agentcore:GetGateway", "bedrock-agentcore:GetGatewayTarget", "bedrock-agentcore:ListGatewayTargets"],
                                                      resources=[gw.attr_gateway_arn]))
        portal_role.add_to_policy(iam.PolicyStatement(actions=["bedrock-agentcore:GetOauth2CredentialProvider", "bedrock-agentcore:ListOauth2CredentialProviders"],
                                                      resources=[f"arn:aws:bedrock-agentcore:{region}:{acct}:token-vault/default/oauth2credentialprovider/*",
                                                                 f"arn:aws:bedrock-agentcore:{region}:{acct}:token-vault/default"]))
        # The portal's token actions take the token vault, the credential provider, the workload identity directory and a
        # workload identity as resources (Service Authorization Reference for Amazon Bedrock AgentCore), so they name this
        # stack's two providers, the default directory and the gateway's workload identity. That identity has the gateway ID
        # as its name: CloudTrail shows the portal role calling GetWorkloadAccessTokenForJWT with it, so the portal stores
        # each token where the Gateway reads it. Unlike the Gateway role, this role need not exist before the Gateway, so the
        # ID comes from the Gateway itself on the first deploy. The guide's sample policy grants these actions on "*".
        portal_role.add_to_policy(iam.PolicyStatement(actions=["bedrock-agentcore:CompleteResourceTokenAuth", "bedrock-agentcore:GetResourceOauth2Token",
                                                               "bedrock-agentcore:GetWorkloadAccessTokenForJWT"],
                                                      resources=[wid_dir, f"{wid_dir}/workload-identity/{gw.attr_gateway_identifier}",
                                                                 f"arn:aws:bedrock-agentcore:{region}:{acct}:token-vault/default",
                                                                 staff_idp.attr_credential_provider_arn, partner_prov.attr_credential_provider_arn]))
        portal_role.add_to_policy(iam.PolicyStatement(actions=["secretsmanager:GetSecretValue"],
                                                      resources=[f"arn:aws:secretsmanager:{region}:{acct}:secret:bedrock-agentcore-identity!default/oauth2/*"],
                                                      conditions={"StringEquals": {"aws:ResourceTag/aws:secretsmanager:owningService": "bedrock-agentcore-identity"}}))
        cr_fn.add_to_role_policy(iam.PolicyStatement(actions=["iam:PassRole"], resources=[portal_role.role_arn],
                                                     conditions={"StringEquals": {"iam:PassedToService": "bedrock-agentcore.amazonaws.com"}}))
        # The portal custom resource calls CreateConsentPortal (with tags, so TagResource too), GetConsentPortal and
        # DeleteConsentPortal. A new portal's ID is not known before it exists, so create and tag-on-create take the
        # consent-portal resource type in this account and Region; get and delete take only this stack's own portal, whose
        # ID starts with its name. The read actions on the gateway and the two credential providers name those resources.
        portal_arns = f"arn:aws:bedrock-agentcore:{region}:{acct}:consent-portal"
        cr_fn.add_to_role_policy(iam.PolicyStatement(actions=["bedrock-agentcore:CreateConsentPortal", "bedrock-agentcore:TagResource"],
                                                     resources=[f"{portal_arns}/*"]))
        cr_fn.add_to_role_policy(iam.PolicyStatement(actions=["bedrock-agentcore:GetConsentPortal", "bedrock-agentcore:DeleteConsentPortal"],
                                                     resources=[f"{portal_arns}/{self.stack_name}-portal-*"]))
        cr_fn.add_to_role_policy(iam.PolicyStatement(actions=["bedrock-agentcore:GetGateway", "bedrock-agentcore:ListGatewayTargets",
                                                              "bedrock-agentcore:GetGatewayTarget"], resources=[gw.attr_gateway_arn]))
        cr_fn.add_to_role_policy(iam.PolicyStatement(actions=["bedrock-agentcore:GetOauth2CredentialProvider"],
                                                     resources=[staff_idp.attr_credential_provider_arn, partner_prov.attr_credential_provider_arn,
                                                                f"arn:aws:bedrock-agentcore:{region}:{acct}:token-vault/default"]))
        portal = CustomResource(self, "ConsentPortal", service_token=provider.service_token, resource_type="Custom::GkConsentPortal",
                                properties={"Kind": "portal", "Name": f"{self.stack_name}-portal", "Description": "Consent portal for the gk demo gateway",
                                            "ExecutionRoleArn": portal_role.role_arn, "IdpProviderArn": staff_idp.attr_credential_provider_arn,
                                            "Scopes": ["openid"], "GatewayId": gw.attr_gateway_identifier, "PortalUrlParam": portal_p.parameter_name})
        portal.node.add_dependency(portal_role, member_target)
        portal_url = portal.get_att_string("PortalUrl")

        staff_cb = CustomResource(self, "PortalLoginCallback", service_token=provider.service_token, resource_type="Custom::GkClientCallback",
                                  properties={"Kind": "callback", "UserPoolId": staff.user_pool_id, "ClientId": portal_login.user_pool_client_id,
                                              "CallbackUrls": [Fn.join("", [portal_url, "/callback"])]})
        partner_cb = CustomResource(self, "PartnerClientCallback", service_token=provider.service_token, resource_type="Custom::GkClientCallback",
                                    properties={"Kind": "callback", "UserPoolId": partner.user_pool_id, "ClientId": partner_client.user_pool_client_id,
                                                "CallbackUrls": [partner_prov.attr_callback_url]})

        openapi = {"openapi": "3.0.1", "info": {"title": "Counseling partner referrals", "version": "1.0.0"},
                   "servers": [{"url": api.api_endpoint}],
                   "paths": {"/referrals": {"get": {"operationId": "partner_list_referrals",
                                                    "summary": "List my open referrals from the financial-counseling partner",
                                                    "description": "Returns the referrals the counseling partner has assigned to the signed-in staff member.",
                                                    "responses": {"200": {"description": "The caller's referrals", "content": {"application/json": {"schema": {
                                                        "type": "object", "properties": {
                                                            "owner": {"type": "string"}, "count": {"type": "integer"}, "digest": {"type": "string"},
                                                            "referrals": {"type": "array", "items": {"type": "object", "properties": {
                                                                "referral_id": {"type": "string"}, "member_id": {"type": "string"}, "title": {"type": "string"},
                                                                "body": {"type": "string"}, "status": {"type": "string"}, "created": {"type": "string"}}}}}}}}}}}}}}
        partner_target = GT(self, "PartnerTarget", name="Partner", gateway_identifier=gw.attr_gateway_identifier,
                            description="Counseling partner referrals (OpenAPI, 3LO)",
                            target_configuration=GT.TargetConfigurationProperty(mcp=GT.McpTargetConfigurationProperty(
                                open_api_schema=GT.ApiSchemaConfigurationProperty(inline_payload=self.to_json_string(openapi)))),
                            credential_provider_configurations=[GT.CredentialProviderConfigurationProperty(credential_provider_type="OAUTH",
                                credential_provider=GT.CredentialProviderProperty(oauth_credential_provider=GT.OAuthCredentialProviderProperty(
                                    provider_arn=partner_prov.attr_credential_provider_arn, scopes=["referrals/read"], grant_type="AUTHORIZATION_CODE",
                                    default_return_url=Fn.join("", [portal_url, "/connect/callback"]))))])
        partner_target.node.add_dependency(portal, partner_cb, staff_cb)

        # ---------------- policies ----------------
        gw_arn = gw.attr_gateway_arn
        res = f'resource == AgentCore::Gateway::"{gw_arn}"'
        act = _act

        statements = demo_statements
        stmts_v2 = statements(res)

        # ---------------- generation 2 policies ----------------
        # The four demo policies. Names carry the generation's suffix: policy names are unique per account and Region,
        # not per engine, and a failed policy keeps its name. Template modes are the end state (every control on);
        # the step scripts and tests switch modes with UpdatePolicy (validationMode IGNORE_ALL_FINDINGS).
        # Generation 3 and later pin the gateway with its literal ARN when the stack's gateway ID is known from context
        # (cdk.json "stacks": {"<stack>": {"gateway_id": ...}}), so no statement references GetAtt Gateway.GatewayArn and a
        # Gateway update cannot re-apply them. The unpinned scope `resource is AgentCore::Gateway` is rejected
        # for tool-specific policies. Without a known ID (a fresh install) they use GetAtt.
        gw_id_ctx = ((self.node.try_get_context("stacks") or {}).get(self.stack_name) or {}).get("gateway_id")
        gw_arn_literal = f"arn:{self.partition}:bedrock-agentcore:{region}:{acct}:gateway/{gw_id_ctx}" if gw_id_ctx else None
        self.policies_by_gen = {}
        for g in gens:
            spec = GENERATIONS[g]
            made = {}
            if spec.pinned and not spec.policy_files:  # generation 2: the statements in policy_sources.py
                items = [(f"{base}{spec.suffix}", kind, stmt) for base, (kind, _mode_v1, stmt) in stmts_v2.items()]
            else:
                scope = f'resource == AgentCore::Gateway::"{gw_arn_literal or gw_arn}"'
                items = [(f"{base}{spec.suffix}", kind, load_statement(fname).replace("resource is AgentCore::Gateway", scope))
                         for base, fname, kind in spec.policy_files]
            for name, kind, stmt in items:
                d = ac.CfnPolicy.PolicyDefinitionProperty(cedar=ac.CfnPolicy.CedarPolicyProperty(statement=stmt)) if kind == "cedar" else \
                    ac.CfnPolicy.PolicyDefinitionProperty(policy=ac.CfnPolicy.PolicyStatementProperty(statement=stmt))
                p = ac.CfnPolicy(self, name, name=name, policy_engine_id=engines[g].attr_policy_engine_id, definition=d, enforcement_mode="ACTIVE",
                                 validation_mode="IGNORE_ALL_FINDINGS")
                p.add_dependency(member_target)
                if g == 2:
                    p.add_dependency(gw)
                else:
                    p.add_dependency(partner_target)
                if g != active_gen:
                    p.apply_removal_policy(RemovalPolicy.RETAIN)
                made[name] = p
                CfnOutput(self, f"Policy{name.replace('_', '')}", value=p.attr_policy_id)
            self.policies_by_gen[g] = made
        if active_gen != 2:
            CfnOutput(self, "ActivePolicies", value=",".join(self.policies_by_gen[active_gen]))

        CfnOutput(self, "GatewayPolicyEngine", value=engine_active.attr_policy_engine_arn)

        # ---------------- outputs (IDs only; no secrets) ----------------

        # Gateway application logs to CloudWatch Logs. Off by default: DEBUG application logs carry full request and
        # response bodies, including `_credential`. Turn on with `-c gw_logs=on`. Every resource here is RETAIN, so
        # turning the flag off orphans them instead of deleting them: delete them by hand before you turn it on again.
        if str(self.node.try_get_context("gw_logs") or "off").lower() in ("on", "true", "1"):
            gw_logs = logs.LogGroup(self, "GatewayVendedLogs", log_group_name=f"/aws/vendedlogs/bedrock-agentcore/{self.stack_name}-gw",
                                    retention=logs.RetentionDays.ONE_WEEK, removal_policy=RemovalPolicy.RETAIN)
            src = logs.CfnDeliverySource(self, "GwLogSource", name=f"{self.stack_name}-gw-app-logs", log_type="APPLICATION_LOGS", resource_arn=gw_arn)
            dst = logs.CfnDeliveryDestination(self, "GwLogDest", name=f"{self.stack_name}-gw-app-logs-cwl", destination_resource_arn=gw_logs.log_group_arn)
            dl = logs.CfnDelivery(self, "GwLogDelivery", delivery_source_name=src.name, delivery_destination_arn=dst.attr_arn)
            dl.add_dependency(src)
            dl.add_dependency(dst)
            for r in (src, dst, dl):
                r.apply_removal_policy(RemovalPolicy.RETAIN)

        # Per-staff request-rate limit on the MemberRecords target. Template 10 per second: a
        # normal agent turn (2 to 4 tool calls within a second) fits under it; the stage demo lowers it to 2 for M6.
        RL = ac.CfnGatewayRateLimit
        rl = RL(self, "PerStaffLimit", gateway_identifier=gw.attr_gateway_identifier, rate_limit_id="per-staff-member-records",
                description="Per staff member request rate on MemberRecords", dimension_keys=["targetName", "$.context.jwt.sub"],
                entries=[RL.LimitEntryProperty(dimensions={"targetName": "MemberRecords", "$.context.jwt.sub": "*"},
                                               requests=[RL.RateConfigProperty(rate=10, period="second")])])
        rl.add_dependency(member_target)
        CfnOutput(self, "RateLimitRef", value=rl.ref)

        for k, v in {"GatewayUrl": gw.attr_gateway_url, "GatewayId": gw.attr_gateway_identifier, "GatewayArn": gw_arn,
                     "EngineId": engine_active.attr_policy_engine_id, "PortalUrl": portal_url, "PortalId": portal.get_att_string("PortalId"),
                     "StaffPoolId": staff.user_pool_id, "PartnerPoolId": partner.user_pool_id, "ConsoleClientId": console.user_pool_client_id,
                     "ApproverClientId": approver.user_pool_client_id, "PortalLoginClientId": portal_login.user_pool_client_id,
                     "PartnerClientId": partner_client.user_pool_client_id, "PartnerApiUrl": api.api_endpoint,
                     "StaffDomain": staff_domain.base_url(), "PartnerDomain": partner_domain.base_url(),
                     "PartnerProviderCallback": partner_prov.attr_callback_url, "ResetFunction": reset_fn.function_name,
                     "ToolsFunction": tools_fn.function_name}.items():
            CfnOutput(self, k, value=v)
