"""Okto Neuron — local-first knowledge graph."""

from pydantic import ValidationError

from okto_neuron.core.schema import Authority, Edge, Item, Mention, Reference, Work
from okto_neuron.errors import (
    ExportError,
    FileNotUnderVaultError,
    IngestError,
    InvalidVaultConfigError,
    OktoNeuronError,
    QueryError,
    VaultAlreadyExistsError,
    VaultClosedError,
    VaultError,
    VaultLockedError,
    VaultNotFoundError,
)
from okto_neuron.models import Document, ExportScope, IngestResult, Node, Provenance, QueryHit
from okto_neuron.primitives import (
    Activity,
    Agent,
    Concept,
    InformationObject,
    Place,
)
from okto_neuron.vault import Vault

__version__ = "0.3.3"
__all__ = [
    "Vault",
    "QueryHit",
    "Provenance",
    "IngestResult",
    "ExportScope",
    "Node",
    "Document",
    "OktoNeuronError",
    "VaultError",
    "VaultNotFoundError",
    "VaultLockedError",
    "VaultAlreadyExistsError",
    "VaultClosedError",
    "InvalidVaultConfigError",
    "IngestError",
    "FileNotUnderVaultError",
    "QueryError",
    "ExportError",
    "ValidationError",
    "Edge",
    "Authority",
    "Mention",
    "Reference",
    "Work",
    "Item",
    "Agent",
    "Activity",
    "InformationObject",
    "Concept",
    "Place",
]
