"""HTTP driver over the existing engine functions.

This package exposes the engine as a FastAPI service for the web app. It is a thin
driver: it reuses the SAME production stack the CLI builds (PostgresStore + the
SanitizingGateway with Mistral providers) and the SAME engine functions. It does not
reimplement any engine logic, and it never bypasses the sanitization gateway.
"""
