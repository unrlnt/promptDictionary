"""promptdict — learn prompting patterns from your own AI chat history, privately.

The core package is stdlib-only. Cloud features (NER sanitization, embeddings,
extraction) live behind optional dependencies; install them with the ``cloud``
extra. All text leaving the device goes through ``promptdict.cloud.SanitizingGateway``.
"""

__version__ = "0.1.0"
