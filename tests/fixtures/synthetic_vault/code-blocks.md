---
title: Code Blocks
tags: [commonmark, parser, blocks]
---

# Code Block Coverage

The ingest pipeline should create a code-block Block for fenced examples.

```python
def claim_id(block_hash, subject, predicate, obj):
    return "deterministic"
```

The code block is source material, not an executable instruction.
