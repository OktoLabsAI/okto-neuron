---
title: Security Fence
tags: [security, n1, local-first]
---

# N1 Security Fence

Vault initialization sets the vault directory to chmod 700.
The default configuration keeps federation disabled and stores graph data under `.marginalia`.

Private client data stays local unless a future step explicitly opts into API egress.
