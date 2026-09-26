from okto_neuron.packs.registry import Pack

PACK = Pack(
    name="personal",
    node_types=("Person", "Event", "Place", "Org", "Conversation", "Commitment"),
    edge_types=("attended", "located_in", "member_of", "knows", "committed_to", "spoke_with"),
    description="People / events / commitments pack.",
)
