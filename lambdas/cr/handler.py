"""Custom resources for the demo stack (Provider framework on_event handlers).

users     create Cognito users with passwords read from Secrets Manager (never from properties)
portal    create the AgentCore consent portal (no CloudFormation type), wait for ACTIVE,
          publish its URL to SSM; delete it only when the stack is deleted
callback  register a callback URL on a Cognito app client, sending the full client config
          (UpdateUserPoolClient resets omitted fields)
Update never changes a physical ID, so CloudFormation never issues a Delete during updates.
"""
import json
import time

import boto3

COG = boto3.client("cognito-idp")
SM = boto3.client("secretsmanager")
SSM = boto3.client("ssm")

CLIENT_FIELDS = ["ClientName", "RefreshTokenValidity", "AccessTokenValidity", "IdTokenValidity", "TokenValidityUnits",
                 "ReadAttributes", "WriteAttributes", "ExplicitAuthFlows", "SupportedIdentityProviders", "LogoutURLs",
                 "AllowedOAuthFlows", "AllowedOAuthScopes", "AllowedOAuthFlowsUserPoolClient", "AnalyticsConfiguration",
                 "PreventUserExistenceErrors", "EnableTokenRevocation", "EnablePropagateAdditionalUserContextData",
                 "AuthSessionValidity", "RefreshTokenRotation"]


def users(event):
    p = event["ResourceProperties"]
    if event["RequestType"] != "Delete":
        for u in p["Users"]:
            pw = SM.get_secret_value(SecretId=u["SecretArn"])["SecretString"]
            try:
                COG.admin_create_user(UserPoolId=u["PoolId"], Username=u["Username"], MessageAction="SUPPRESS")
            except COG.exceptions.UsernameExistsException:
                pass
            COG.admin_set_user_password(UserPoolId=u["PoolId"], Username=u["Username"], Password=pw, Permanent=True)
            for g in u.get("Groups", []):
                COG.admin_add_user_to_group(UserPoolId=u["PoolId"], Username=u["Username"], GroupName=g)
    return {"PhysicalResourceId": event.get("PhysicalResourceId") or f"gk-users-{p['Name']}"}


def portal(event):
    c = boto3.client("bedrock-agentcore-control")
    p = event["ResourceProperties"]
    rt = event["RequestType"]
    if rt == "Create":
        r = c.create_consent_portal(name=p["Name"], description=p.get("Description", ""), executionRoleArn=p["ExecutionRoleArn"],
                                    idpConfig={"credentialProviderArn": p["IdpProviderArn"], "scopes": p["Scopes"]},
                                    sources=[{"identifier": p["GatewayId"], "type": "agentcore-gateway"}],
                                    tags={"project": "gatekeepers-demo"})
        pid = r["consentPortalId"]
    else:
        pid = event["PhysicalResourceId"]
    if rt == "Delete":  # runs only when the stack is deleted
        try:
            c.delete_consent_portal(consentPortalIdentifier=pid)
        except c.exceptions.ResourceNotFoundException:
            return {"PhysicalResourceId": pid}
        for _ in range(80):
            try:
                c.get_consent_portal(consentPortalIdentifier=pid)
            except c.exceptions.ResourceNotFoundException:
                break
            time.sleep(10)
        return {"PhysicalResourceId": pid}
    for _ in range(80):
        g = c.get_consent_portal(consentPortalIdentifier=pid)
        if g["status"] == "ACTIVE" and g.get("portalUrl"):
            break
        if g["status"] in ("FAILED", "UPDATE_FAILED"):
            raise RuntimeError(f"consent portal {g['status']}: {g.get('statusReason')}")
        time.sleep(10)
    else:
        raise RuntimeError("consent portal did not become ACTIVE in time")
    url = g["portalUrl"].rstrip("/")
    SSM.put_parameter(Name=p["PortalUrlParam"], Value=url, Type="String", Overwrite=True)
    return {"PhysicalResourceId": pid, "Data": {"PortalUrl": url, "PortalId": pid, "PortalArn": g["consentPortalArn"]}}


def callback(event):
    p = event["ResourceProperties"]
    if event["RequestType"] != "Delete":
        d = COG.describe_user_pool_client(UserPoolId=p["UserPoolId"], ClientId=p["ClientId"])["UserPoolClient"]
        kw = {k: d[k] for k in CLIENT_FIELDS if k in d}
        kw["CallbackURLs"] = p["CallbackUrls"]
        COG.update_user_pool_client(UserPoolId=p["UserPoolId"], ClientId=p["ClientId"], **kw)
        after = COG.describe_user_pool_client(UserPoolId=p["UserPoolId"], ClientId=p["ClientId"])["UserPoolClient"]
        print(json.dumps({"client": p["ClientId"], "callbacks": after.get("CallbackURLs"), "flows": after.get("AllowedOAuthFlows"),
                          "scopes": after.get("AllowedOAuthScopes")}))
    return {"PhysicalResourceId": event.get("PhysicalResourceId") or f"gk-callback-{p['ClientId']}"}


def handler(event, context):
    kind = event["ResourceProperties"]["Kind"]
    return {"users": users, "portal": portal, "callback": callback}[kind](event)
