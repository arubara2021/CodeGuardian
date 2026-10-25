"""Bundled context provider plugins.

One directory per provider, each with a ``plugin.py`` module. A
context provider returns a text snippet the agent inserts into its
prompt. Unlike a tool, which returns structured data the agent may
inspect or transform, a context provider's output is text the agent
reads directly.
"""
