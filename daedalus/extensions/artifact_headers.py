"""Browser boundaries for artifact bytes, including direct navigation outside the preview."""

ACTIVE_DOCUMENTS = frozenset({"text/html", "application/xhtml+xml", "image/svg+xml"})
DOCUMENT_POLICY = (
    "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'; img-src data: blob:; media-src data: blob:; "
    "font-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'"
)


def artifact_headers(mime: str) -> dict[str, str]:
    headers = {"X-Content-Type-Options": "nosniff"}
    if mime.partition(";")[0].strip().lower() in ACTIVE_DOCUMENTS:
        # Opening a kept HTML or SVG directly bypassed the iframe's opaque origin and let its
        # scripts read cookie-authenticated APIs. The response itself must keep that boundary.
        headers["Content-Security-Policy"] = DOCUMENT_POLICY
    return headers
