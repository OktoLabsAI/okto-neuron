import pytest

from okto_neuron.models.ner import GLiNERNER


def test_retired_gliner_surface_fails_actionably_without_importing_gliner():
    with pytest.warns(DeprecationWarning, match="not part of Okto Neuron"):
        extractor = GLiNERNER()

    with pytest.raises(RuntimeError, match="configured LLM extraction pipeline"):
        extractor.extract("Marie Curie discovered radium.", ["Person"])
