"""Pack registry — node types and edge types contributed by a pack."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Pack:
    name: str
    node_types: tuple[str, ...] = ()
    edge_types: tuple[str, ...] = ()
    description: str = ""


@dataclass
class PackRegistry:
    packs: dict[str, Pack] = field(default_factory=dict)

    def add(self, pack: Pack) -> None:
        self.packs[pack.name] = pack

    def all_node_types(self) -> set[str]:
        out: set[str] = set()
        for p in self.packs.values():
            out.update(p.node_types)
        return out

    def all_edge_types(self) -> set[str]:
        out: set[str] = set()
        for p in self.packs.values():
            out.update(p.edge_types)
        return out

    def has_node_type(self, t: str) -> bool:
        return t in self.all_node_types()


def load_packs(names: list[str]) -> PackRegistry:
    from okto_neuron.packs import BUILTIN

    reg = PackRegistry()
    for n in names:
        if n not in BUILTIN:
            raise ValueError(f"unknown pack: {n}")
        reg.add(BUILTIN[n])
    return reg
