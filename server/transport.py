"""HTTP transport helpers shared by the local WebUI request handler."""

from __future__ import annotations

import json
from email.parser import BytesParser
from email.policy import default as email_policy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path


def json_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0"))
    raw = handler.rfile.read(length) if length else b"{}"
    return json.loads(raw.decode("utf-8"))


def multipart_body(handler: BaseHTTPRequestHandler) -> dict[str, tuple[str, bytes] | str]:
    """Parse browser FormData without the removed Python 3.13 cgi module."""
    content_type = handler.headers.get("Content-Type", "")
    if "multipart/form-data" not in content_type:
        raise ValueError("需要 multipart/form-data 上传")
    length = int(handler.headers.get("Content-Length", "0"))
    raw = handler.rfile.read(length)
    envelope = (
        f"Content-Type: {content_type}\r\n"
        "MIME-Version: 1.0\r\n"
        "\r\n"
    ).encode("utf-8") + raw
    message = BytesParser(policy=email_policy).parsebytes(envelope)
    fields: dict[str, tuple[str, bytes] | str] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        value = part.get_payload(decode=True) or b""
        if filename is not None:
            fields[name] = (filename, value)
        else:
            charset = part.get_content_charset() or "utf-8"
            fields[name] = value.decode(charset, errors="replace")
    return fields


def send_json(handler: BaseHTTPRequestHandler, payload: dict, status: int = 200) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def send_bytes(handler: BaseHTTPRequestHandler, data: bytes, content_type: str) -> None:
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)


def send_attachment(handler: BaseHTTPRequestHandler, data: bytes, filename: str,
                    content_type: str = "application/octet-stream") -> None:
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Disposition", f'attachment; filename="{filename}"')
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)


def send_file_range(handler: BaseHTTPRequestHandler, path: Path, content_type: str) -> None:
    """Serve a file with byte-range support so media elements can seek."""
    size = path.stat().st_size
    range_header = handler.headers.get("Range", "").strip()
    start = 0
    end = size - 1
    partial = False
    if range_header:
        if not range_header.lower().startswith("bytes=") or "," in range_header:
            _send_range_error(handler, size)
            return
        first, separator, last = range_header[6:].strip().partition("-")
        try:
            if not separator:
                raise ValueError
            if first:
                start = int(first)
                if start < 0 or start >= size:
                    raise ValueError
                end = int(last) if last else size - 1
                if end < start:
                    raise ValueError
                end = min(end, size - 1)
            else:
                suffix_length = int(last)
                if suffix_length <= 0 or size == 0:
                    raise ValueError
                start = max(size - suffix_length, 0)
                end = size - 1
        except (TypeError, ValueError):
            _send_range_error(handler, size)
            return
        partial = True

    length = max(0, end - start + 1)
    handler.send_response(HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Accept-Ranges", "bytes")
    handler.send_header("Content-Length", str(length))
    handler.send_header("Cache-Control", "no-store")
    if partial:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    handler.end_headers()
    if length == 0:
        return
    with path.open("rb") as stream:
        stream.seek(start)
        remaining = length
        while remaining:
            chunk = stream.read(min(64 * 1024, remaining))
            if not chunk:
                break
            handler.wfile.write(chunk)
            remaining -= len(chunk)


def _send_range_error(handler: BaseHTTPRequestHandler, size: int) -> None:
    handler.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
    handler.send_header("Content-Range", f"bytes */{size}")
    handler.send_header("Content-Length", "0")
    handler.end_headers()
