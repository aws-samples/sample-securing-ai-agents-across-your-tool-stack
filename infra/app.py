"""CDK app for the Gatekeepers and Guardrails demo (one stack).

Context values (all optional):
  stack_name   stack name; default gk-demo
  region       default us-east-2 (the agent uses the US cross-Region inference profile for Claude Sonnet 4.6)
  sfx          suffix of the two Cognito managed-login domains, gk-staff-<sfx> and gk-partner-<sfx>. Domain prefixes are
               unique in a Region across all AWS accounts, so the default is derived from the account, the Region and
               the stack name. Pass -c sfx=<4 to 12 lowercase letters or digits> to choose your own.
  policy_gens  the policy engine generations in the template; default 2 (gk_demo/generations.py)
  policy_gen   the generation the Gateway uses; default 2
  users_sync   any value that changes on every deploy (scripts/deploy.sh passes the epoch), so the users
               custom resource re-syncs Cognito passwords from Secrets Manager
  gw_logs      on: deliver the Gateway's application logs to CloudWatch Logs (off by default, see gk_demo/stack.py)

The account comes from the CDK environment (the credentials in use), never from a file.
"""
import hashlib
import os
import re

import aws_cdk as cdk

from gk_demo.stack import GkDemoStack

app = cdk.App()
name = app.node.try_get_context("stack_name") or "gk-demo"
region = app.node.try_get_context("region") or "us-east-2"
account = app.node.try_get_context("account") or os.environ.get("CDK_DEFAULT_ACCOUNT")
sfx = app.node.try_get_context("sfx")
if not sfx:
    if not account:
        raise SystemExit("No AWS account: run with AWS credentials, or pass -c sfx=<suffix> (and -c account=<id> to synth offline)")
    sfx = hashlib.sha256(f"{account}/{region}/{name}".encode()).hexdigest()[:8]
if not re.fullmatch(r"[a-z0-9]{4,12}", str(sfx)):
    raise SystemExit(f"sfx must be 4 to 12 lowercase letters or digits, not {sfx!r}")
app.node.set_context("sfx", str(sfx))
GkDemoStack(app, name, env=cdk.Environment(account=account, region=region), description=f"Gatekeepers and Guardrails demo ({name})")
cdk.Tags.of(app).add("project", "gatekeepers-demo")
app.synth()
