"""M5: the destination rule ACTIVE. Start new conversations in both panes afterwards (old sessions get -32005)."""
from steplib import switch

switch("DestinationSeenInSession", "ACTIVE", "destination rule ACTIVE")
print("Start new conversations in both panes.")
