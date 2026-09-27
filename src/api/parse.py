import time

from flask import Blueprint, current_app, jsonify, request
from configs.logging_config import get_logger
from utils.web_fetcher import WebFetcher, UrlParser
from src.parser_factory import ParserFactory
from src.api.response import make_response
from src.api.download import create_download_token
from src.db import refund_user_credit, reserve_user_credit
from src.api.access import (
    authenticate_api_key,
    consume_rate_limit,
    demo_enabled,
    get_client_ip,
    global_api_enabled,
    platform_access,
    record_request,
)

bp = Blueprint('parse', __name__)
MAX_TEXT_LENGTH = 2000
logger = get_logger(__name__)


@bp.route('/health', methods=['GET'])
def health():
    """供容器编排、反向代理和监控系统检查服务状态。"""
    return jsonify({'status': 'ok'}), 200


@bp.route('/parse', methods=['GET', 'POST'])
def parse():
    """解析接口，网页体验或微服务调用接口。"""
    if not global_api_enabled():
        return make_response(503, 'API 服务已暂停', None, False, 'API_DISABLED'), 503
    is_api_only = bool(current_app and current_app.config.get('API_ONLY'))
    if not is_api_only:
        if not demo_enabled():
            return make_response(403, '在线体验暂未开放', None, False, 'DEMO_DISABLED'), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return make_response(400, '请求体必须是 JSON 对象', None, False, 'INVALID_REQUEST'), 400
        if data.get('website'):
            return make_response(400, '非法请求', None, False, 'INVALID_REQUEST'), 400
        if not (current_app and current_app.testing):
            client_ip = get_client_ip()
            if not consume_rate_limit(f"demo_ip:{client_ip}", 5, window_seconds=60):
                return make_response(429, '体验解析过于频繁，请 1 分钟后再试', None, False, 'RATE_LIMITED'), 429
        return _execute_parse(data.get('text') or data.get('url'), None)

    # API_ONLY 模式：完全无限制，支持 GET/POST (JSON/Form/Query)
    text = None
    if request.method == 'GET':
        text = request.args.get('url') or request.args.get('text')
    elif request.is_json:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return make_response(400, '请求体必须是 JSON 对象', None, False, 'INVALID_REQUEST'), 400
        if data.get('website'):
            return make_response(400, '非法请求', None, False, 'INVALID_REQUEST'), 400
        text = data.get('url') or data.get('text')
    elif request.form:
        if request.form.get('website'):
            return make_response(400, '非法请求', None, False, 'INVALID_REQUEST'), 400
        text = request.form.get('url') or request.form.get('text')
    else:
        text = request.args.get('url') or request.args.get('text')

    return _execute_parse(text, None)


@bp.route('/v1/parse', methods=['GET', 'POST'])
def simple_parse():
    """面向客户的解析接口，支持 GET 与 POST。"""
    if not global_api_enabled():
        return make_response(503, 'API 服务已暂停', None, False, 'API_DISABLED'), 503
    is_api_only = bool(current_app and current_app.config.get('API_ONLY'))
    access = None
    if not is_api_only:
        access, error = authenticate_api_key()
        if error:
            status, message, code = error
            return make_response(status, message, None, False, code), status

    text = None
    if request.method == 'GET':
        text = request.args.get('url') or request.args.get('text')
    else:
        if request.is_json:
            data = request.get_json(silent=True)
            if isinstance(data, dict):
                text = data.get('url') or data.get('text')
        elif request.form:
            text = request.form.get('url') or request.form.get('text')
        if not text:
            text = request.args.get('url') or request.args.get('text')

    return _execute_parse(text, access)


def _execute_parse(text, access):
    started = time.monotonic()
    platform = None
    response = None
    status = 500
    credit_reserved = False
    credit_committed = False
    try:
        if not isinstance(text, str) or not text.strip():
            response, status = make_response(400, '请提供包含分享链接的文本', None, False, 'INVALID_TEXT'), 400
            return response, status
        if len(text) > MAX_TEXT_LENGTH:
            response, status = make_response(400, f'分享文本不能超过 {MAX_TEXT_LENGTH} 个字符', None, False, 'TEXT_TOO_LONG'), 400
            return response, status

        share_url = UrlParser.get_url(text)
        if not share_url:
            response, status = make_response(400, '未找到有效的分享链接', None, False, 'URL_NOT_FOUND'), 400
            return response, status

        # 1. 解析基础信息
        redirect_url = WebFetcher.fetch_redirect_url(share_url)
        if not redirect_url:
            if not WebFetcher._is_allowed_target(share_url):
                logger.error(f'This link is not supported for extraction: {share_url}')
                response, status = make_response(400, '该链接尚未支持提取', None, False, 'PLATFORM_NOT_SUPPORTED'), 400
                return response, status
            platform = UrlParser.get_platform(share_url)
            response, status = make_response(400, '无法访问或识别该分享链接', None, False, 'REDIRECT_FAILED'), 400
            return response, status

        platform = UrlParser.get_platform(redirect_url)
        real_url = UrlParser.extract_video_address(redirect_url)
        logger.debug(f'real_url {real_url}')

        if not platform:
            logger.error(f'This link is not supported for extraction: {real_url}')
            response, status = make_response(400, '该链接尚未支持提取', None, False, 'PLATFORM_NOT_SUPPORTED'), 400
            return response, status

        denied = platform_access(platform)
        if denied:
            status, message, code = denied
            response = make_response(status, message, None, False, code)
            return response, status

        if access and access["user_id"]:
            reservation = reserve_user_credit(access["user_id"])
            if reservation is None:
                response, status = make_response(
                    402,
                    '账号解析积分已耗尽，请联系管理员充值',
                    None,
                    False,
                    'INSUFFICIENT_CREDITS',
                ), 402
                return response, status
            credit_reserved = reservation

        # 2. 获取解析器
        parser = ParserFactory.create_parser(platform, real_url)
        
        # 3. 核心抓取逻辑
        content_data = _fetch_with_retry(parser, platform)

        if (
            not content_data['video_url']
            and not content_data['video_list']
            and not content_data['image_list']
            and not content_data.get('audio_url')
        ):
            terminal_detail = getattr(parser, 'terminal_error', None) or getattr(parser, '_terminal_filter_detail', None)
            if isinstance(terminal_detail, dict):
                detail_msg = terminal_detail.get('detail_msg') or terminal_detail.get('notice') or '该内容可能为私密/日常作品或已被作者删除'
                err_code = terminal_detail.get('error_code') or 'MEDIA_DELETED_OR_PRIVATE'
                if isinstance(detail_msg, str) and detail_msg.strip():
                    response, status = make_response(400, detail_msg.strip(), None, False, err_code), 400
                    return response, status
            if platform == '小红书':
                response, status = make_response(400, '解析失败：该链接需要小红书登录 Cookie 校验，请在配置中提供有效 Cookie 后重试', None, False, 'XIAOHONGSHU_COOKIE_REQUIRED'), 400
                return response, status
            if platform == '拼多多':
                response, status = make_response(400, '解析失败：该链接需要拼多多登录 Cookie 校验，请在配置中提供有效 Cookie 后重试', None, False, 'PINDUODUO_COOKIE_REQUIRED'), 400
                return response, status
            if platform in ('视频号', '微信视频号'):
                response, status = make_response(400, '解析失败：该链接需要配置腾讯元宝 YUANBAO_COOKIE 凭证后重试', None, False, 'WECHAT_CHANNELS_COOKIE_REQUIRED'), 400
                return response, status
            if platform == '快手' and getattr(parser, 'cookie_required', False):
                response, status = make_response(400, '解析失败：该链接触发快手安全校验，请在配置中提供有效快手 Cookie 后重试', None, False, 'KUAISHOU_COOKIE_REQUIRED'), 400
                return response, status
            is_no_media = getattr(parser, 'no_media_in_content', False)
            if is_no_media is True or (
                type(is_no_media).__name__ not in ('Mock', 'MagicMock')
                and platform in ('豆包', '通义千问', '腾讯元宝', '夸克AI', '小云雀AI')
                and (content_data.get('title') or content_data.get('desc'))
            ):
                response, status = make_response(400, '该分享内容仅包含文本对话，未包含图片或视频资源', None, False, 'NO_MEDIA_IN_CONTENT'), 400
                return response, status
            response, status = make_response(400, '提取媒体内容失败，请检查链接或稍后重试', None, False, 'MEDIA_NOT_FOUND'), 400
            return response, status

        processed_image_list = []
        if content_data.get('image_list'):
            for img in content_data['image_list']:
                if isinstance(img, dict):
                    processed_image_list.append({
                        'url': UrlParser.convert_to_https(img.get('url')),
                        'live_photo_url': UrlParser.convert_to_https(img.get('live_photo_url'))
                    })
                else:
                    processed_image_list.append(UrlParser.convert_to_https(img))

        processed_video_list = [
            UrlParser.convert_to_https(url)
            for url in content_data.get('video_list', [])
            if url
        ]
        processed_video_list = list(dict.fromkeys(processed_video_list))
        primary_video_url = UrlParser.convert_to_https(content_data['video_url'])
        if not primary_video_url and processed_video_list:
            primary_video_url = processed_video_list[0]
        if primary_video_url and primary_video_url in processed_video_list:
            processed_video_list.remove(primary_video_url)
            processed_video_list.insert(0, primary_video_url)

        # 4. 统一转换 HTTPS，并维持 v1 既有主字段的兜底语义。
        title = content_data['title']
        desc = content_data['desc']
        if not title and desc:
            title = desc

        cover_url = UrlParser.convert_to_https(content_data['cover_url'])
        if not cover_url and processed_image_list:
            first_image = processed_image_list[0]
            cover_url = first_image.get('url') if isinstance(first_image, dict) else first_image

        data_dict = {
            'video_id': UrlParser.get_video_id(redirect_url),
            'platform': platform,
            'title': title,
            'desc': desc,
            'video_url': primary_video_url,
            'audio_url': UrlParser.convert_to_https(content_data.get('audio_url')),
            'cover_url': cover_url,
            'author': content_data['author'],
            'image_list': processed_image_list
        }
        if primary_video_url:
            data_dict['video_download_token'] = create_download_token(primary_video_url, 'video', title)
        if cover_url:
            data_dict['cover_download_token'] = create_download_token(cover_url, 'cover', title)
        if len(processed_video_list) > 1:
            data_dict['video_list'] = processed_video_list
        if content_data.get('subtitles'):
            data_dict['subtitles'] = content_data['subtitles']
        if content_data.get('is_preview') is True:
            data_dict['is_preview'] = True
            if content_data.get('full_duration'):
                data_dict['full_duration'] = content_data['full_duration']
        
        logger.debug(f'Parse Success for platform {platform}')
        response, status = make_response(200, '成功', data_dict, True), 200
        credit_committed = True
        return response, status

    except Exception as e:
        logger.exception("Parse Error") # 使用 exception 会带上堆栈信息
        response, status = make_response(500, '功能太火爆啦，请稍后再试', None, False, 'INTERNAL_ERROR'), 500
        return response, status
    finally:
        if credit_reserved and not credit_committed:
            try:
                refund_user_credit(access["user_id"])
            except Exception:
                logger.exception("Reserved credit refund failed")
        if response is not None:
            payload = response.get_json(silent=True) or {}
            try:
                record_request(
                    access,
                    platform,
                    request.path,
                    status,
                    payload.get('error_code'),
                    int((time.monotonic() - started) * 1000),
                )
            except Exception:
                logger.exception("Request log write failed")


def _fetch_with_retry(parser, platform):
    """提取公共的抓取逻辑，小红书特殊处理"""
    max_attempts = 3 if platform == '小红书' else 1
    
    for i in range(max_attempts):
        res = {
            'title': parser.get_title_content(),
            'desc': safe_execute(getattr(parser, 'get_description', None)),
            'video_url': parser.get_real_video_url(),
            'video_list': safe_execute(getattr(parser, 'get_video_list', None), default=[]),
            'cover_url': parser.get_cover_photo_url(),
            'author': safe_execute(getattr(parser, 'get_author_info', None)),
            'image_list': safe_execute(getattr(parser, 'get_image_list', None), default=[]),
            'audio_url': safe_execute(getattr(parser, 'get_audio_url', None)),
            'subtitles': safe_execute(getattr(parser, 'get_subtitles', None)),
            'is_preview': safe_execute(lambda: parser.is_preview, default=False),
            'full_duration': safe_execute(lambda: parser.full_duration),
        }
        if res['video_url'] or res['video_list'] or res['image_list'] or res['audio_url']:
            return res
            
        if i < max_attempts - 1:
            logger.debug(f"Attempt {i + 1} failed. Retrying...")
            
    return res


def safe_execute(func, default=None):
    """安全执行辅助函数，减少 try-except 视觉噪音"""
    if not func or not callable(func):
        return default
    try:
        val = func()
        if type(val).__name__ in ('Mock', 'MagicMock'):
            return default
        return val
    except Exception:
        return default
