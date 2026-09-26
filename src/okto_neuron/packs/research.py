from okto_neuron.packs.registry import Pack

PACK = Pack(
    name="research",
    node_types=("Note", "Source", "Claim", "Topic", "Quote", "Question", "Finding"),
    edge_types=("supports", "contradicts", "cites", "answers", "derived_from"),
    description="Research / notes / literature pack.",
)
