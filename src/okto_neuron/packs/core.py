"""Core pack — always loaded. The ultra-generic substrate."""

from okto_neuron.packs.registry import Pack

PACK = Pack(
    name="core",
    node_types=("Node", "Authority", "Work", "Item"),
    edge_types=(
        "mentions",
        "references",
        "related",
        "supersedes",
        "broader",
        "narrower",
        "exactMatch",
        "closeMatch",
    ),
    description="Ultra-generic substrate (FRBR + SKOS).",
)
