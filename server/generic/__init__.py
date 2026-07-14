"""Generic provider mode: builds chat-completions requests and streams
responses against any of the four supported upstreams.

AER mode lives next door in :mod:`server.aer` and uses the raw-text
``/v1/completions`` endpoint with AER's prompt-formatting conventions. Generic
mode talks ``/v1/chat/completions`` against NovelAI generic / OpenRouter /
NanoGPT / openai-compatible, with the prompt assembled from a user-authored
``ContextPreset`` rather than AER's hardcoded template.
"""
