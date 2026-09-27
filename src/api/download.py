import base64
import hashlib
import hmac
import ipaddress
import json
import mimetypes
import socket
import time
from urllib.parse import urljoin, urlsplit

import requests
from flask import Blueprint, Response, current_app, jsonify, request, stream_with_context


bp = Blueprint("download", __name__)
TOKEN_TTL_SECONDS = 24 * 60 * 60
MAX_TOKEN_LENGTH = 8192
MAX_REDIRECTS = 5
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
CHUNK_SIZE = 64 * 1024
MAX_VIDEO_BYTES = 512 * 1024 * 1024
MAX_COVER_BYTES = 32 * 1024 * 1024


def create_download_token(media_url, media_type):
    """Create a short-lived signed token for media discovered by the parser."""
    if not media_url or media_type not in {"video", "cover"}:
        return None
    _validate_url_shape(media_url)
    payload = {
        "url": media_url,
        "type": media_type,
        "exp": int(time.time()) + TOKEN_TTL_SECONDS,
    }
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
        upstream = requests.get(
            current_url,
            headers={"User-Agent": "Mozilla/5.0 mini-parse media download"},
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
    max_bytes = MAX_VIDEO_BYTES if payload["type"] == "video" else MAX_COVER_BYTES
    content_length = upstream.headers.get("Content-Length")
    if content_length and content_length.isdigit() and int(content_length) > max_bytes:
        upstream.close()
        return jsonify({"error": "媒体文件超过允许大小"}), 413
    headers = {
        "Content-Type": content_type,
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": _content_disposition(payload["type"], content_type),
    }
    if content_length and content_length.isdigit():
        headers["Content-Length"] = content_length
    for name in ("Accept-Ranges", "Content-Range"):
        if name in upstream.headers:
            headers[name] = upstream.headers[name]

    def stream_content():
        streamed_bytes = 0
        try:
            for chunk in upstream.iter_content(chunk_size=CHUNK_SIZE):
                streamed_bytes += len(chunk)
                if streamed_bytes > max_bytes:
                    raise ValueError("Media exceeds the maximum download size")
                yield chunk
        finally:
            upstream.close()

    return Response(
        stream_with_context(stream_content()),
        status=upstream.status_code,
        headers=headers,
        direct_passthrough=True,
    )


def _content_disposition(media_type, content_type):
    if media_type == "video":
        extension = ".mp4"
    else:
        extension = mimetypes.guess_extension(content_type) or ".jpg"
        if extension == ".jpe":
            extension = ".jpg"
    return f'attachment; filename="mini-parse-{media_type}{extension}"'
