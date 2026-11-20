#!/usr/bin/env bash
# Deploy the demo stack with a diff first ("Before every cdk deploy").
#
#   scripts/deploy.sh diff <label> [-c key=value ...]   cdk diff -> logs/diff-<label>.log; saves the context for the deploy
#   scripts/deploy.sh deploy <label>                    cdk deploy with the same saved context -> logs/deploy-<label>.log
#   GK_STACK, GK_REGION: stack name and Region (default gk-demo, us-east-2); GK_DEPLOY_ARGS: deploy-only flags
#
# Every deploy passes a fresh users_sync value, so the users custom resource re-syncs Cognito passwords.
# Read the whole diff before deploying. After the deploy: scripts/state.py on (it ends with a check).
set -euo pipefail
cd "$(dirname "$0")/../infra"
. "$HOME/.nvm/nvm.sh" >/dev/null 2>&1 || true
export PYTHONDONTWRITEBYTECODE=1
STACK="${GK_STACK:-gk-demo}"
REGION="${GK_REGION:-us-east-2}"  # passed to the app as -c region
LOGS=../logs
mkdir -p "$LOGS"
cmd="${1:?diff|deploy}"
label="${2:?label}"
shift 2
ctx="$LOGS/deploy-$label.ctx"
# Bytecode inside a Lambda asset directory changes that asset's hash, so the diff would show a code update that is not a
# source change. Refuse instead of guessing.
if [ -n "$(find ../lambdas -name __pycache__ -o -name '*.pyc' | head -1)" ]; then
  echo "refusing: bytecode under lambdas/ would change the Lambda asset hashes:"
  find ../lambdas -name __pycache__ -o -name '*.pyc'
  echo "remove it, and run Python with PYTHONDONTWRITEBYTECODE=1 near the Lambda sources"
  exit 3
fi
# The Lambda layers and the agent's code bundle. Idempotent: unchanged inputs give the same directories, so the same asset hashes.
bash ../scripts/build_layers.sh
echo "agent bundle: $(bash ../scripts/build_agent.sh)"
case "$cmd" in
  diff)
    printf '%s\n' "-c" "stack_name=$STACK" "-c" "region=$REGION" "-c" "users_sync=$(date +%s)" "$@" > "$ctx"
    readarray -t args < "$ctx"
    cdk diff "$STACK" "${args[@]}" > "$LOGS/diff-$label.log" 2>&1 || true
    echo "diff written to $LOGS/diff-$label.log"
    ;;
  deploy)
    readarray -t args < "$ctx"
    # GK_DEPLOY_ARGS: deploy-only CDK flags, for example --no-rollback for a first create, so a failed create keeps its
    # resources for a retry instead of CloudFormation deleting them.
    read -r -a extra <<< "${GK_DEPLOY_ARGS:-}"
    cdk deploy "$STACK" "${args[@]}" "${extra[@]}" --require-approval never > "$LOGS/deploy-$label.log" 2>&1
    echo "deploy finished, log $LOGS/deploy-$label.log"
    ;;
  *)
    echo "usage: $0 diff|deploy <label> [-c key=value ...]"
    exit 2
    ;;
esac
