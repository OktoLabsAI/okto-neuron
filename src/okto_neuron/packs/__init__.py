"""Type packs — domain vocabularies layered on top of core."""

from okto_neuron.packs import core as core_pack
from okto_neuron.packs import personal as personal_pack
from okto_neuron.packs import research as research_pack
from okto_neuron.packs import sdlc as sdlc_pack
from okto_neuron.packs.registry import Pack, PackRegistry, load_packs

BUILTIN: dict[str, Pack] = {
    "core": core_pack.PACK,
    "research": research_pack.PACK,
    "personal": personal_pack.PACK,
    "sdlc": sdlc_pack.PACK,
}

__all__ = ["Pack", "PackRegistry", "load_packs", "BUILTIN"]
