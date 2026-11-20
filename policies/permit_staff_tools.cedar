// Base permit. Cedar denies by default. Unpinned scope: any gateway this engine is attached to.
permit (principal, action, resource is AgentCore::Gateway);
