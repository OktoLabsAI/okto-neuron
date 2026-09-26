import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
ADR = ROOT / "docs" / "adr" / "0001-rejected-models.md"
README = ROOT / "README.md"
RFC = ROOT / "RFC.md"


def test_adr_file_exists():
    assert ADR.is_file()


def test_adr_has_five_rejected_model_sections():
    text = ADR.read_text(encoding="utf-8")
    headings = re.findall(r"^(?:#{2,3})\s+(.+)$", text, re.MULTILINE)
    model_keywords = ["OpenIE", "e5-mistral", "jina", "REBEL", "CoreML"]
    found = [k for k in model_keywords if any(k.lower() in h.lower() for h in headings)]
    assert len(found) == 5, f"expected 5 rejected models, found {found}"


def test_adr_linked_from_readme():
    assert "0001-rejected-models.md" in README.read_text(encoding="utf-8")


def test_adr_linked_from_rfc():
    assert "0001-rejected-models.md" in RFC.read_text(encoding="utf-8")
