"""M4 step 2: the SSN guardrail policy ACTIVE."""
from steplib import switch

switch("SuppressSsnInNotes", "ACTIVE", "SSN guardrail ACTIVE")
