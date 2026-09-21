"""Sam's Multi-Model Router (Phase 13).

One trusted, deterministic boundary between Sam and every language-model
provider. The router is NOT an authorization boundary: PermissionEngine stays
the only authority, and provider output is untrusted text.
"""
