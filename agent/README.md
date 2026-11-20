# agent/

Member Navigator, the [Strands Agents](https://strandsagents.com/) agent that runs on Amazon Bedrock AgentCore Runtime with direct code deployment.

- `main.py`: the agent. It forwards the caller's bearer token unchanged to the AgentCore Gateway, with the conversation's policy session ID, and opens one MCP client per turn. Hooks classify every tool result by its JSON-RPC error code and turn the Gateway's decisions into typed stream events for the console. The agent logs only those classifications: no prompts, results, arguments, headers or tokens.
- `gk_mcp/`: the MCP client module for the Gateway and the result classifier, shared by the agent and the scripts' direct-call path.
- `requirements.in`: the top-level dependencies. `scripts/build_agent.sh` compiles them into `requirements.txt` for Linux arm64 and Python 3.12 and builds the code bundle in `agent/.build/` (git-ignored). `scripts/deploy.sh` runs it before every diff and deploy.

The Runtime's execution role (`infra/gk_demo/agent_runtime.py`) allows model invocation and the Runtime's own needs only. The agent has no permission on the Gateway: the Gateway authorizes the forwarded staff JWT, not IAM.
