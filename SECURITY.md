# Security Policy

## 1. Security Architecture Principles

`durallm` is designed to be a lightweight, self-hostable resilience gateway for autonomous AI agents. Its architecture adheres to strict security invariants:

1. **Zero Phone-Home / Air-Gapped Operation**:
   The gateway contains zero third-party telemetry, analytics, or remote tracking. Telemetry stays entirely in-process and in your local environment.
2. **Credential Isolation**:
   API keys for upstream providers (Anthropic, OpenAI, Cerebras, Groq, Mistral, OpenRouter, NVIDIA, Gemini) are read exclusively from environment variables or secure server-side headers. Keys are never logged, echoed in error responses, or exposed to downstream clients.
3. **Secure Header Transport (Zero URL Credential Leaks)**:
   In compliance with security audit remediation, Google Gemini API keys are passed exclusively via the `x-goog-api-key` HTTP header. API keys are strictly prohibited from appearing in URL query strings, preventing credential leakage in proxy access logs, browser history, or intermediate proxies.
4. **Zero Global Process Mutation**:
   The gateway does not call `socket.setdefaulttimeout()` or alter process-wide network state. Timeouts are strictly enforced per HTTP connection.
5. **Fail-Closed Semantic Tool Validation (Rule 3)**:
   The gateway validates all model-generated tool calls against schema definitions. It strictly forbids "guessing" or synthesizing missing parameters. Unparseable outputs are rejected before they can reach local system execution environments (such as bash shells).

---

## 2. Reporting a Vulnerability

If you discover a security vulnerability in `durallm`, please do not open a public issue.

Please report security concerns responsibly:
- **Email:** `deepak@users.noreply.github.com`
- **Subject:** `[SECURITY] durallm Vulnerability Report`

Please include:
- A description of the vulnerability and its potential impact.
- Reproducible steps or a proof of concept.
- Affected versions or configurations.

We will acknowledge receipt within 48 hours and work with you to remediate and publish a coordinated security advisory.
