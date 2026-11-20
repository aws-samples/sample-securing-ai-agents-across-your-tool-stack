"""Policy engine generations.

A generation is one policy engine plus its policy set. Engine and policy names carry the generation's suffix because
policy names are unique per account and Region and a failed policy keeps its name. Moving to a new generation creates
the new engine and policies next to the old ones and points the Gateway at the new engine (`-c policy_gen=N`); older
generations stay in the template with RemovalPolicy.RETAIN until you delete them by hand.

Generation 2 is the demo's policy set: four statements scoped to the gateway ARN, text in policy_sources.py. It is the
only generation the demo deploys (infra/cdk.json).
Generation 3 loads its statements from ../../policies/ and scopes them to this stack's gateway (stack.py). Its set also
holds a prompt-attack guardrail policy; the demo runs that check in the RESPONSE interceptor instead.
"""
import os
from dataclasses import dataclass, field

POLICIES_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "policies"))

# Generation 3's policy set, read from ../../policies/.
G3_SET = [  # (base name, file, kind)
    ("gk_PermitAll", "permit_staff_tools.cedar", "cedar"),
    ("gk_DestinationSeenInSession", "destination_seen_in_session.dw", "policy"),
    ("gk_ApprovalOver2500", "approval_over_2500.dw", "policy"),
    ("gk_SuppressSsnInNotes", "suppress_ssn_in_notes.policy", "policy"),
    ("gk_PromptAttackOnReferrals", "prompt_attack_on_referrals.policy", "policy"),
]


def load_statement(filename):
    """The deployed statement: the file without its `//` comment lines."""
    with open(os.path.join(POLICIES_DIR, filename)) as f:
        return "\n".join(ln for ln in f.read().splitlines() if not ln.lstrip().startswith("//")).strip()


@dataclass
class Generation:
    number: int
    engine_cid: str
    engine_name: str
    engine_description: str
    suffix: str
    pinned: bool
    policy_files: list = field(default_factory=list)  # (base, file, kind) for file-based generations


GENERATIONS = {
    2: Generation(2, "Engine2", "gk_demo_engine2", "gk demo policy engine, generation 2 (the demo policy set)",
                  "_v2", pinned=True),
    3: Generation(3, "EngineG3", "gk_demo_engine_g3", "gk demo policy engine, generation 3 (unpinned scope)",
                  "_g3", pinned=False, policy_files=G3_SET),
}
