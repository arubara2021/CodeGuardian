"""Bundled plugins that ship with CodeGuardian.

Each subpackage (``languages``, ``tools``, ``reporters``) contains one
directory per plugin. A plugin directory holds a ``plugin.py`` module
whose class carries a ``__manifest__`` dict. Discovery walks these
directories at startup; no ``__all__`` re-export is needed here.
"""
