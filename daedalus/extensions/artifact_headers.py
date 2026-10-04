"""Browser boundaries for artifact bytes, including direct navigation outside the preview."""

ACTIVE_DOCUMENTS = frozenset({"text/html", "application/xhtml+xml", "image/svg+xml", "text/xml", "application/xml"})
DOCUMENT_POLICY = (
    "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'; img-src data: blob:; media-src data: blob:; "
    "font-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'"
)


def artifact_headers(mime: str) -> dict[str, str]:
    headers = {"X-Content-Type-Options": "nosniff"}
    kind = mime.partition(";")[0].strip().lower()
    if kind in ACTIVE_DOCUMENTS or kind.endswith("+xml"):
        # SVG markup served as text/xml also executes on direct navigation. The response must
        # keep the opaque-origin boundary regardless of the document's XML media type.
        headers["Content-Security-Policy"] = DOCUMENT_POLICY
    return headers
