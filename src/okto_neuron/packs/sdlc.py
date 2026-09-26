from okto_neuron.packs.registry import Pack

PACK = Pack(
    name="sdlc",
    node_types=(
        "Decision",
        "Criterion",
        "Constraint",
        "Requirement",
        "Assumption",
        "Alternative",
        "APIContract",
        "TestScenario",
        "Bug",
        "Learning",
    ),
    edge_types=("implements", "tests", "validates", "violates", "depends_on", "derives_from"),
    description="SDLC pack — mirrors Pulse's current schema for parity / future migration.",
)
