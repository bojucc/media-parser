import base64
import hashlib
import hmac
import ipaddress
import json
import mimetypes
import re
import socket
import threading
import time
from urllib.parse import quote, urljoin, urlsplit

import requests
from flask import Blueprint, Response, current_app, jsonify, request, stream_with_context


bp = Blueprint("download", __name__)
TOKEN_TTL_SECONDS = 24 * 60 * 60
MAX_TOKEN_LENGTH = 8192
MAX_REDIRECTS = 5
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
CHUNK_SIZE = 256 * 1024
MAX_VIDEO_BYTES = 512 * 1024 * 1024
MAX_COVER_BYTES = 32 * 1024 * 1024
_HTTP_LOCAL = threading.local()


def _get_http_session():
    """Reuse upstream keep-alive connections within each Gunicorn thread."""
    session = getattr(_HTTP_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=4)
        session.mount("https://", adapter)
        _HTTP_LOCAL.session = session
    return session


def create_download_token(media_url, media_type, title=None):
    """Create a short-lived signed token for media discovered by the parser."""
    if not media_url or media_type not in {"video", "cover"}:
        return None
    _validate_url_shape(media_url)
    payload = {
        "url": media_url,
        "type": media_type,
        "exp": int(time.time()) + TOKEN_TTL_SECONDS,
    }
    if isinstance(title, str) and title.strip():
        payload["title"] = title.strip()[:200]
    encoded_payload = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    signature = _sign(encoded_payload)
    token = f"{encoded_payload}.{signature}"
    if len(token) > MAX_TOKEN_LENGTH:
        return None
    return token


def _sign(encoded_payload):
    secret = current_app.config["SECRET_KEY"]
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    return hmac.new(secret, encoded_payload.encode("ascii"), hashlib.sha256).hexdigest()


def _decode_token(token):
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LENGTH:
        raise ValueError("Invalid download token")
    try:
        encoded_payload, signature = token.split(".", 1)
        if not hmac.compare_digest(_sign(encoded_payload), signature):
            raise ValueError("Invalid download token")
        padded = encoded_payload + "=" * (-len(encoded_payload) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if payload.get("type") not in {"video", "cover"}:
            raise ValueError("Invalid download token")
        if not isinstance(payload.get("exp"), int) or payload["exp"] < int(time.time()):
            raise ValueError("Expired download token")
        _validate_url_shape(payload.get("url"))
        return payload
    except (TypeError, KeyError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Invalid download token") from error


def _validate_url_shape(url):
    if not isinstance(url, str):
        raise ValueError("Invalid media URL")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.hostname.lower() in {"localhost", "localhost.localdomain"}
    ):
        raise ValueError("Invalid media URL")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return
    if not address.is_global:
        raise ValueError("Invalid media URL")


def _validate_public_target(url):
    _validate_url_shape(url)
    parsed = urlsplit(url)
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except OSError as error:
        raise ValueError("Media host could not be resolved") from error
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("Media host is not public")


def _fetch_media(url):
    current_url = url
    for redirect_count in range(MAX_REDIRECTS + 1):
        _validate_public_target(current_url)
        upstream_headers = {
            "User-Agent": "Mozilla/5.0 mini-parse media download",
            "Accept-Encoding": "identity",
        }
        requested_range = request.headers.get("Range")
        if requested_range:
            upstream_headers["Range"] = requested_range
        upstream = _get_http_session().get(
            current_url,
            headers=upstream_headers,
            stream=True,
            allow_redirects=False,
            timeout=(5, 30),
        )
        if upstream.status_code not in REDIRECT_STATUSES:
            return upstream
        location = upstream.headers.get("Location")
        upstream.close()
        if not location or redirect_count == MAX_REDIRECTS:
            raise ValueError("Invalid media redirect")
        current_url = urljoin(current_url, location)
    raise ValueError("Too many media redirects")


@bp.route("/download", methods=["GET"])
def download():
    try:
        payload = _decode_token(request.headers.get("X-Media-Download-Token"))
    except ValueError:
        return jsonify({"error": "下载凭证无效或已过期"}), 400

    started_at = time.monotonic()
    try:
        upstream = _fetch_media(payload["url"])
    except ValueError:
        return jsonify({"error": "媒体地址不可用"}), 403
    except requests.RequestException:
        return jsonify({"error": "媒体源暂时无法访问"}), 502

    if upstream.status_code not in {200, 206}:
        upstream.close()
        return jsonify({"error": "媒体源暂时无法访问"}), 502

    content_type = upstream.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0]
    try:
        upstream_host = urlsplit(upstream.url).hostname or "unknown"
    except (TypeError, ValueError):
        upstream_host = "unknown"
    max_bytes = MAX_VIDEO_BYTES if payload["type"] == "video" else MAX_COVER_BYTES
    content_length = upstream.headers.get("Content-Length")
    expected_bytes = int(content_length) if content_length and content_length.isdigit() else None
    if content_length and content_length.isdigit() and int(content_length) > max_bytes:
        upstream.close()
        return jsonify({"error": "媒体文件超过允许大小"}), 413
    headers = {
        "Content-Type": content_type,
        "Cache-Control": "private, no-store",
        "X-Accel-Buffering": "no",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": _content_disposition(
            payload["type"], content_type, payload.get("title")
        ),
    }
    if content_length and content_length.isdigit():
        headers["Content-Length"] = content_length
    for name in ("Accept-Ranges", "Content-Range"):
        if name in upstream.headers:
            headers[name] = upstream.headers[name]

    def stream_content():
        streamed_bytes = 0
        first_byte_ms = None
        outcome = "complete"
        try:
            for chunk in upstream.iter_content(chunk_size=CHUNK_SIZE):
                if chunk and first_byte_ms is None:
                    first_byte_ms = round((time.monotonic() - started_at) * 1000)
                streamed_bytes += len(chunk)
                if streamed_bytes > max_bytes:
                    outcome = "size_limit"
                    raise ValueError("Media exceeds the maximum download size")
                yield chunk
            if expected_bytes is not None and streamed_bytes != expected_bytes:
                outcome = "upstream_truncated"
        except GeneratorExit:
            if expected_bytes is None or streamed_bytes < expected_bytes:
                outcome = "client_disconnected"
            raise
        except Exception:
            outcome = "upstream_error"
            raise
        finally:
            upstream.close()
            current_app.logger.info(
                "media_download type=%s host=%s upstream_status=%s first_byte_ms=%s "
                "duration_ms=%s bytes=%s outcome=%s",
                payload["type"],
                upstream_host,
                upstream.status_code,
                first_byte_ms if first_byte_ms is not None else "none",
                round((time.monotonic() - started_at) * 1000),
                streamed_bytes,
                outcome,
            )

    return Response(
        stream_with_context(stream_content()),
        status=upstream.status_code,
        headers=headers,
        direct_passthrough=True,
    )


def _content_disposition(media_type, content_type, title=None):
    if media_type == "video":
        extension = ".mp4"
    else:
        extension = mimetypes.guess_extension(content_type) or ".jpg"
        if extension == ".jpe":
            extension = ".jpg"
    safe_title = _safe_filename(title)
    filename = f"{safe_title}{extension}" if safe_title else f"mini-parse-{media_type}{extension}"
    fallback = f"mini-parse-{media_type}{extension}"
    return (
        f'attachment; filename="{fallback}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )


def _safe_filename(title):
    if not isinstance(title, str):
        return ""
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', "", title).strip().rstrip(".")
    value = re.sub(r"\s+", " ", value)
    return value[:100].rstrip()
