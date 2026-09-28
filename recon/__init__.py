"""Deterministic core of the freight billing reconciliation.

Every rupee figure in the reconciliation report is computed here. Agents
never produce amounts; they compile contracts into rate cards, review
exceptions and write memos, and their outputs are checked by this package.
"""
