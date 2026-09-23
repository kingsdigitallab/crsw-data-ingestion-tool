"""CRSW deposit conventions: keys, dataset record, manifest, object
labels, vocabulary, noise rules. Shared by the command-line tool and
the web deposit service so the two routes cannot drift.

Stdlib only. Python 3.8+. No user I/O anywhere in this package."""

__version__ = "0.6.0"  # tracks the dataset record schema version

from . import keys, record, vocab, labels, noise, deposit_logic  # noqa: E402,F401
