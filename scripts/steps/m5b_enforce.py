"""M5b: the approval rule ACTIVE. Start new conversations in both panes afterwards (old sessions get -32005)."""
from steplib import switch

switch("ApprovalOver2500", "ACTIVE", "approval rule ACTIVE")
print("Start new conversations in both panes.")
