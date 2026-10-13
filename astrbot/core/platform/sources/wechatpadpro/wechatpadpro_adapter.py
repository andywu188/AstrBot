import asyncio
import base64
import json
import os
import time
import traceback
from pathlib import Path
from typing import cast

import aiohttp

from astrbot import logger
from astrbot.api.message_components import At, Image, Plain, Record
from astrbot.api.platform import Platform, PlatformMetadata
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import MessageSesion
from astrbot.core.platform.astrbot_message import (
    AstrBotMessage,
    MessageMember,
    MessageType,
)
from astrbot.core.utils.astrbot_path import (
    get_astrbot_data_path,
    get_astrbot_temp_path,
)

from ...register import register_platform_adapter
from .wechatpadpro_message_event import WeChatPadProMessageEvent

try:
    from .xml_data_parser import GeweDataParser
except ImportError as e:
    logger.warning(
        f"警告: 可能未安装 defusedxml 依赖库，将导致无法解析微信的 表情包、引用 类型的消息: {e!s}",
    )


@register_platform_adapter(
    "wechatpadpro", "WeChatPadPro 消息平台适配器", support_streaming_message=False
)
class WeChatPadProAdapter(Platform):
    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: asyncio.Queue,
    ) -> None:
        super().__init__(platform_config, event_queue)
        self._shutdown_event = None
        self.wxnewpass = None
        self.settings = platform_settings

        self.metadata = PlatformMetadata(
            name="wechatpadpro",
            description="WeChatPadPro 消息平台适配器",
            id=self.config.get("id", "wechatpadpro"),
            support_streaming_message=False,
        )

        # 保存配置信息
        self.admin_key = self.config.get("admin_key")
        self.host = self.config.get("host")
        # ``port`` is shared with webhook callback metadata, so new configs use
        # ``wechatpadpro_port``. Keep ``port`` for configs saved before removal.
        self.port = self.config.get("wechatpadpro_port", self.config.get("port"))
        # WeChatPadProMAX swagger basePath is /api.
        self.base_url = f"http://{self.host}:{self.port}/api"
        self.auth_key = None  # 用于保存生成的授权码
        self.wxid: str | None = None  # 用于保存登录成功后的 wxid
        self.qr_uuid: str | None = None
        self.credentials_file = str(
            Path(get_astrbot_data_path()) / "wechatpadpro_credentials.json",
        )
        self.ws_handle_task = None

        # 添加图片消息缓存，用于引用消息处理
        self.cached_images = {}
        """缓存图片消息。key是NewMsgId (对应引用消息的svrid)，value是图片的base64数据"""
        # 设置缓存大小限制，避免内存占用过大
        self.max_image_cache = 50

        # 添加文本消息缓存，用于引用消息处理
        self.cached_texts = {}
        """缓存文本消息。key是NewMsgId (对应引用消息的svrid)，value是消息文本内容"""
        # 设置文本缓存大小限制
        self.max_text_cache = 100

    async def run(self) -> None:
        """启动平台适配器的运行实例。"""
        logger.info("WeChatPadPro 适配器正在启动...")

        if loaded_credentials := self.load_credentials():
            self.auth_key = loaded_credentials.get("auth_key")
            self.wxid = loaded_credentials.get("wxid")

        isLoginIn = await self.check_online_status()

        # 检查在线状态
        if self.auth_key and isLoginIn:
            logger.info("WeChatPadPro 设备已在线，凭据存在，跳过扫码登录。")
            self.ws_handle_task = asyncio.create_task(self.connect_websocket())
        else:
            # 1. 生成授权码
            if not self.auth_key:
                logger.info("WeChatPadPro 无可用凭据，将生成新的授权码。")
                await self.generate_auth_key()

            # 2. 获取登录二维码
            if not isLoginIn:
                logger.info("WeChatPadPro 设备已离线，开始扫码登录。")
                qr_code_url = await self.get_login_qr_code()

                if qr_code_url:
                    logger.info(f"请扫描以下二维码登录: {qr_code_url}")
                else:
                    logger.error("无法获取登录二维码。")
                    return

                # 3. 检测扫码状态
                login_successful = await self.check_login_status()

                if login_successful:
                    logger.info("登录成功，WeChatPadPro适配器已连接。")
                else:
                    logger.warning("登录失败或超时，WeChatPadPro 适配器将关闭。")
                    await self.terminate()
                    return

            self.ws_handle_task = asyncio.create_task(self.connect_websocket())

        self._shutdown_event = asyncio.Event()
        await self._shutdown_event.wait()
        logger.info("WeChatPadPro 适配器已停止。")

    def load_credentials(self):
        """从文件中加载 auth_key 和 wxid。"""
        if os.path.exists(self.credentials_file):
            try:
                with open(self.credentials_file) as f:
                    credentials = json.load(f)
                    logger.info("成功加载 WeChatPadPro 凭据。")
                    return credentials
            except Exception as e:
                logger.error(f"加载 WeChatPadPro 凭据失败: {e}")
        return None

    def save_credentials(self):
        """将 auth_key 和 wxid 保存到文件。"""
        credentials = {
            "auth_key": self.auth_key,
            "wxid": self.wxid,
        }
        try:
            # 确保数据目录存在
            data_dir = os.path.dirname(self.credentials_file)
            os.makedirs(data_dir, exist_ok=True)
            with open(self.credentials_file, "w") as f:
                json.dump(credentials, f)
        except Exception as e:
            logger.error(f"保存 WeChatPadPro 凭据失败: {e}")

    def _user_params(self) -> dict:
        return {"authcode": self.auth_key}

    @staticmethod
    def _api_ok(status: int, data: dict) -> bool:
        """WeChatPadProMAX uses Code 0; older builds used Code 200."""
        if status != 200 or not isinstance(data, dict) or data.get("Success") is False:
            return False
        return data.get("Code") in (0, 200)

    @staticmethod
    def _is_online(data: dict) -> bool:
        payload = data.get("Data") if isinstance(data.get("Data"), dict) else {}
        if payload.get("online") is True:
            return True
        state = payload.get("loginState")
        return state in (1, "1", "ready", "online")

    async def check_online_status(self):
        """检查 WeChatPadPro 设备是否在线。"""
        if not self.auth_key:
            return False
        url = f"{self.base_url}/Login/GetLoginStatus"
        params = {**self._user_params(), "autoLogin": "true"}

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, params=params) as response:
                    response_data = await response.json()
                    if self._api_ok(response.status, response_data):
                        if self._is_online(response_data):
                            logger.info("WeChatPadPro 设备当前在线。")
                            await self._refresh_wxid()
                            return True
                        logger.info("WeChatPadPro 设备不在线。")
                        return False
                    if response.status == 200 and response_data.get("Code") in (
                        300,
                        -2,
                    ):
                        if response_data.get("Code") == -2:
                            self.auth_key = None
                        logger.info("WeChatPadPro 授权码无效或设备已退出。")
                        return False
                    logger.error(
                        f"检查在线状态失败: {response.status}, {response_data}",
                    )
                    return False

            except aiohttp.ClientConnectorError as e:
                logger.error(f"连接到 WeChatPadPro 服务失败: {e}")
                return False
            except Exception as e:
                logger.error(f"检查在线状态时发生错误: {e}")
                logger.error(traceback.format_exc())
                return False

    def _extract_auth_key(self, data):
        """Helper method to extract auth_key from response data."""
        if isinstance(data, dict):
            for key in ("authKeys", "AuthKeys", "authcodes", "authcode"):
                auth_keys = data.get(key)
                if isinstance(auth_keys, list) and auth_keys:
                    return auth_keys[0]
                if isinstance(auth_keys, str) and auth_keys:
                    return auth_keys
        elif isinstance(data, list) and data:
            return data[0]
        elif isinstance(data, str) and data:
            return data
        return None

    async def generate_auth_key(self):
        """生成授权码。"""
        url = f"{self.base_url}/Admin/GenAuthKey"
        params = {"key": self.admin_key}
        payload = {"count": 1, "days": 365, "remark": "astrbot"}

        self.auth_key = None  # Reset auth_key before generating a new one

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, params=params, json=payload) as response:
                    if response.status != 200:
                        logger.error(
                            f"生成授权码失败: {response.status}, {await response.text()}",
                        )
                        return

                    response_data = await response.json()
                    if self._api_ok(response.status, response_data):
                        if data := response_data.get("Data"):
                            self.auth_key = self._extract_auth_key(data)

                        if self.auth_key:
                            logger.info("成功获取授权码")
                        else:
                            logger.error(
                                f"生成授权码成功但未找到授权码: {response_data}",
                            )
                    else:
                        logger.error(f"生成授权码失败: {response_data}")
            except aiohttp.ClientConnectorError as e:
                logger.error(f"连接到 WeChatPadPro 服务失败: {e}")
            except Exception as e:
                logger.error(f"生成授权码时发生错误: {e}")

    async def get_login_qr_code(self):
        """获取登录二维码地址。"""
        url = f"{self.base_url}/Login/GetQR"
        params = self._user_params()
        payload = {"DeviceName": "AstrBot", "oversea": False}

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, params=params, json=payload) as response:
                    response_data = await response.json()
                    if self._api_ok(response.status, response_data):
                        data = response_data.get("Data") or {}
                        self.qr_uuid = data.get("uuid") or data.get("Uuid")
                        qr_url = data.get("qr_code_url") or data.get("QrCodeUrl")
                        if qr_url:
                            return qr_url
                        logger.error(
                            f"获取登录二维码成功但未找到二维码地址: {response_data}",
                        )
                        return None
                    text = str(
                        response_data.get("Text") or response_data.get("Message") or "",
                    )
                    if "该 key 无效" in text or ("授权" in text and "无效" in text):
                        logger.error(
                            "授权码无效，已经清除。请重新启动 AstrBot 或者本消息适配器。原因也可能是 WeChatPadPro 的 MySQL 服务没有启动成功，请检查 WeChatPadPro 服务的日志。",
                        )
                        self.auth_key = None
                        self.save_credentials()
                        return None
                    logger.error(
                        f"获取登录二维码失败: {response.status}, {response_data}",
                    )
                    return None
            except aiohttp.ClientConnectorError as e:
                logger.error(f"连接到 WeChatPadPro 服务失败: {e}")
                return None
            except Exception as e:
                logger.error(f"获取登录二维码时发生错误: {e}")
                return None

    async def check_login_status(self):
        """循环检测扫码状态。
        尝试 6 次后跳出循环，添加倒计时。
        返回 True 如果登录成功，否则返回 False。
        """
        url = f"{self.base_url}/Login/CheckQR"
        params = {**self._user_params()}
        if self.qr_uuid:
            params["uuid"] = self.qr_uuid

        attempts = 0  # 初始化尝试次数
        max_attempts = 36  # 最大尝试次数
        countdown = 180  # 倒计时时长
        logger.info(f"请在 {countdown} 秒内扫码登录。")
        while attempts < max_attempts:
            async with aiohttp.ClientSession() as session:
                try:
                    if await self.check_online_status():
                        await self._refresh_wxid()
                        self.save_credentials()
                        return True
                    async with session.post(url, params=params, json={}) as response:
                        response_data = await response.json()
                        if self._api_ok(response.status, response_data):
                            data = response_data.get("Data") or {}
                            status = data.get("state", data.get("loginState"))
                            logger.info(
                                f"第 {attempts + 1} 次尝试，当前登录状态: {status}，还剩{countdown - attempts * 5}秒",
                            )
                            if status in (2, "2") or self._is_online(response_data):
                                self.wxid = data.get("wxid") or self.wxid
                                await self._refresh_wxid()
                                logger.info(f"登录成功，wxid: {self.wxid}")
                                self.save_credentials()
                                return True
                            if status in (-2, "-2"):
                                logger.error("二维码已过期，请重新获取。")
                                return False
                        elif response_data.get("Code") == 300:
                            pass
                        else:
                            logger.info(
                                f"检测登录状态失败: {response.status}, {response_data}",
                            )

                except aiohttp.ClientConnectorError as e:
                    logger.error(f"连接到 WeChatPadPro 服务失败: {e}")
                    await asyncio.sleep(5)
                    attempts += 1
                    continue
                except Exception as e:
                    logger.error(f"检测登录状态时发生错误: {e}")
                    attempts += 1
                    continue

            attempts += 1
            await asyncio.sleep(5)  # 每隔5秒检测一次
        logger.warning("登录检测超过最大尝试次数，退出检测。")
        return False

    async def _refresh_wxid(self) -> None:
        """Read wxid from the documented login cache."""
        if self.wxid or not self.auth_key:
            return
        url = f"{self.base_url}/Login/GetCacheInfo"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    url, params=self._user_params(), json={}
                ) as response:
                    data = await response.json()
                    if self._api_ok(response.status, data):
                        payload = data.get("Data") or {}
                        self.wxid = payload.get("wxid") or self.wxid
            except Exception as e:
                logger.error(f"获取登录缓存失败: {e}")

    async def connect_websocket(self) -> None:
        """Receive messages from /ws/sync.

        Each connection uses a one-time ticket from /Runtime/WebSocketTicket.
        A dropped socket fetches a new ticket and connects again.
        """
        logger.info("WeChatPadPro is receiving messages over WebSocket.")
        reconnect_delay = 1
        while self._shutdown_event is None or not self._shutdown_event.is_set():
            try:
                await self._listen_sync_ws()
                reconnect_delay = 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    "WeChatPadPro WebSocket failed: %s. Check the service.",
                    e,
                )
            if self._shutdown_event is not None and self._shutdown_event.is_set():
                break
            logger.info(
                "WeChatPadPro WebSocket reconnects in %s seconds.",
                reconnect_delay,
            )
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 30)

    async def _fetch_ws_ticket(self) -> str:
        """Exchange a single-use ticket valid for 5 minutes.

        Returns:
            Ticket string for /ws/sync.

        Raises:
            RuntimeError: The ticket endpoint did not return a ticket.
        """
        url = f"{self.base_url}/Runtime/WebSocketTicket"
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                params=self._user_params(),
                headers={"X-Access-Token": self.auth_key or ""},
                json={},
            ) as response:
                data = await response.json()
        if not self._api_ok(response.status, data):
            raise RuntimeError(f"ticket request failed: {response.status}, {data}")
        payload = data.get("Data")
        ticket = None
        if isinstance(payload, dict):
            ticket = payload.get("ticket") or payload.get("Ticket")
        elif isinstance(payload, str):
            ticket = payload
        if not ticket:
            raise RuntimeError("ticket response did not include a ticket")
        return str(ticket)

    async def _listen_sync_ws(self) -> None:
        """Open one /ws/sync connection and read until it closes."""
        ticket = await self._fetch_ws_ticket()
        ws_url = f"ws://{self.host}:{self.port}/ws/sync"
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.ws_connect(
                ws_url,
                params={"ticket": ticket},
                heartbeat=30.0,
                autoping=True,
            ) as ws:
                logger.info("WeChatPadPro WebSocket connected.")
                async for message in ws:
                    if message.type in {
                        aiohttp.WSMsgType.TEXT,
                        aiohttp.WSMsgType.BINARY,
                    }:
                        await self._consume_ws_frame(message.data)
                        continue
                    if message.type in {
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.ERROR,
                    }:
                        logger.warning("WeChatPadPro WebSocket closed.")
                        break

    async def _consume_ws_frame(self, raw: str | bytes) -> None:
        """Parse one WebSocket frame and hand messages to the existing handler."""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("WeChatPadPro ignored a non-JSON WebSocket frame.")
            return
        await self._consume_ws_payload(payload)

    async def _consume_ws_payload(self, payload: object, depth: int = 0) -> None:
        """Walk a sync frame until each message reaches handle_websocket_message.

        Args:
            payload: Decoded JSON value from one frame.
            depth: Nesting depth, used to stop on unexpected wrappers.
        """
        if depth > 6:
            return
        if isinstance(payload, list):
            for item in payload:
                await self._consume_ws_payload(item, depth + 1)
            return
        if not isinstance(payload, dict):
            return
        if (
            payload.get("msg_id") is not None
            and payload.get("from_user_name") is not None
        ):
            await self.handle_websocket_message(json.dumps(payload))
            return
        normalized = self._normalize_sync_item(payload)
        if normalized is not None:
            await self.handle_websocket_message(json.dumps(normalized))
            return
        for key in (
            "Data",
            "data",
            "items",
            "AddMsgs",
            "messages",
            "AddMsg",
            "message",
        ):
            if key in payload:
                await self._consume_ws_payload(payload[key], depth + 1)
                return
        logger.debug(
            "WeChatPadPro ignored a WebSocket payload with keys: %s",
            list(payload)[:12],
        )

    def _normalize_sync_item(self, item: dict) -> dict | None:
        """Map a sync item onto the fields convert_message already reads."""
        if (
            isinstance(item.get("from_user_name"), dict)
            and item.get("msg_id") is not None
        ):
            return item
        reply = (
            item.get("reply_context")
            if isinstance(item.get("reply_context"), dict)
            else {}
        )
        from_id = item.get("from_user_id") or reply.get("from_user_id") or ""
        if isinstance(from_id, dict):
            from_id = from_id.get("str") or ""
        chat = (
            item.get("chat_user_id")
            or item.get("to_wxid")
            or reply.get("chat_user_id")
            or reply.get("to_wxid")
            or ""
        )
        content = item.get("content") or item.get("text") or ""
        if not isinstance(content, str):
            content = str(content or "")
        msg_id = item.get("msg_id") or item.get("new_msg_id") or reply.get("new_msg_id")
        if msg_id is None or not from_id:
            return None
        room = str(chat) if str(chat).endswith("@chatroom") else ""
        if str(from_id).endswith("@chatroom"):
            sender = str(item.get("sender_id") or reply.get("from_user_id") or "")
            text = content if ":\n" in content else f"{sender}:\n{content}"
            from_name = str(from_id)
        elif room:
            text = (
                content
                if content.startswith(f"{from_id}:\n")
                else f"{from_id}:\n{content}"
            )
            from_name = room
        else:
            text = content
            from_name = str(from_id)
        at_users = item.get("at_user_list") or item.get("AtUserList") or []
        msg_source = item.get("msg_source") or ""
        if (
            isinstance(at_users, list)
            and at_users
            and "<atuserlist>" not in str(msg_source)
        ):
            msg_source = (
                f"<atuserlist>{','.join(str(user) for user in at_users)}</atuserlist>"
            )
        return {
            "msg_id": msg_id,
            "new_msg_id": str(
                item.get("new_msg_id") or reply.get("new_msg_id") or msg_id
            ),
            "create_time": item.get("create_time")
            or item.get("created_at")
            or int(time.time()),
            "from_user_name": {"str": from_name},
            "to_user_name": {"str": self.wxid or str(chat)},
            "content": {"str": text},
            "msg_type": item.get("msg_type")
            or item.get("message_type")
            or reply.get("msg_type")
            or 1,
            "msg_source": msg_source,
            "push_content": item.get("push_content") or "",
        }

    def _find_member_nickname(self, node, wxid: str) -> str | None:
        if isinstance(node, dict):
            uid = node.get("user_name") or node.get("wxid") or node.get("userName")
            nick = (
                node.get("nick_name")
                or node.get("nickname")
                or node.get("display_name")
            )
            if uid == wxid and nick:
                return str(nick)
            for value in node.values():
                found = self._find_member_nickname(value, wxid)
                if found:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = self._find_member_nickname(value, wxid)
                if found:
                    return found
        return None

    async def handle_websocket_message(self, message: str | bytes):
        """处理从 WebSocket 接收到的消息。"""
        logger.debug(f"收到 WebSocket 消息: {message}")
        try:
            message_data = json.loads(message)
            if (
                message_data.get("msg_id") is not None
                and message_data.get("from_user_name") is not None
            ):
                abm = await self.convert_message(message_data)
                if abm:
                    # 创建 WeChatPadProMessageEvent 实例
                    message_event = WeChatPadProMessageEvent(
                        message_str=abm.message_str,
                        message_obj=abm,
                        platform_meta=self.meta(),
                        session_id=abm.session_id,
                        # 传递适配器实例，以便在事件中调用 send 方法
                        adapter=self,
                    )
                    # 提交事件到事件队列
                    self.commit_event(message_event)
            else:
                logger.warning(f"收到未知结构的 WebSocket 消息: {message_data}")

        except json.JSONDecodeError:
            logger.error(f"无法解析 WebSocket 消息为 JSON: {message}")
        except Exception as e:
            logger.error(f"处理 WebSocket 消息时发生错误: {e}")

    async def convert_message(self, raw_message: dict) -> AstrBotMessage | None:
        """将 WeChatPadPro 原始消息转换为 AstrBotMessage。"""
        if self.wxid is None:
            logger.error("WeChatPadPro 适配器未登录或未获取到 wxid，无法处理消息。")
            return None
        abm = AstrBotMessage()
        abm.raw_message = raw_message
        abm.message_id = str(raw_message.get("msg_id"))
        abm.timestamp = cast(int, raw_message.get("create_time") or int(time.time()))
        abm.self_id = self.wxid

        if int(time.time()) - abm.timestamp > 180:
            logger.warning(
                f"忽略 3 分钟前的旧消息：消息时间戳 {abm.timestamp} 超过当前时间 {int(time.time())}。",
            )
            return None

        from_user_name = raw_message.get("from_user_name", {}).get("str", "")
        to_user_name = raw_message.get("to_user_name", {}).get("str", "")
        content = raw_message.get("content", {}).get("str", "")
        push_content = raw_message.get("push_content", "")
        msg_type = cast(int, raw_message.get("msg_type"))

        abm.message_str = ""
        abm.message = []

        # 如果是机器人自己发送的消息、回显消息或系统消息，忽略
        if from_user_name == self.wxid:
            logger.info("忽略来自自己的消息。")
            return None

        if from_user_name in ["weixin", "newsapp", "newsapp_wechat"]:
            logger.info("忽略来自微信团队的消息。")
            return None

        # 先判断群聊/私聊并设置基本属性
        if await self._process_chat_type(
            abm,
            raw_message,
            from_user_name,
            to_user_name,
            content,
            push_content,
        ):
            # 再根据消息类型处理消息内容
            await self._process_message_content(abm, raw_message, msg_type, content)

            return abm
        return None

    async def _process_chat_type(
        self,
        abm: AstrBotMessage,
        raw_message: dict,
        from_user_name: str,
        to_user_name: str,
        content: str,
        push_content: str,
    ):
        """判断消息是群聊还是私聊，并设置 AstrBotMessage 的基本属性。"""
        if from_user_name == "weixin":
            return False
        at_me = False
        if "@chatroom" in from_user_name:
            abm.type = MessageType.GROUP_MESSAGE
            abm.group_id = from_user_name

            parts = content.split(":\n", 1)
            sender_wxid = parts[0] if len(parts) == 2 else ""
            abm.sender = MessageMember(user_id=sender_wxid, nickname="")

            # 获取群聊发送者的nickname
            if sender_wxid:
                accurate_nickname = await self._get_group_member_nickname(
                    abm.group_id,
                    sender_wxid,
                )
                if accurate_nickname:
                    abm.sender.nickname = accurate_nickname

            if abm.type == MessageType.GROUP_MESSAGE:
                abm.session_id = abm.group_id
            else:
                abm.session_id = abm.sender.user_id

            msg_source = raw_message.get("msg_source", "")
            if self.wxid in msg_source:
                at_me = True
            if "在群聊中@了你" in raw_message.get("push_content", ""):
                at_me = True
            if at_me:
                abm.message.insert(0, At(qq=abm.self_id, name=""))
        else:
            abm.type = MessageType.FRIEND_MESSAGE
            abm.group_id = ""
            nick_name = ""
            if push_content and " : " in push_content:
                nick_name = push_content.split(" : ")[0]
            abm.sender = MessageMember(user_id=from_user_name, nickname=nick_name)
            abm.session_id = from_user_name
        return True

    async def _get_group_member_nickname(
        self,
        group_id: str,
        member_wxid: str,
    ) -> str | None:
        """通过接口获取群成员的昵称。"""
        url = f"{self.base_url}/Group/GetChatRoomMemberDetail"
        params = self._user_params()
        payload = {"QID": group_id}

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, params=params, json=payload) as response:
                    response_data = await response.json()
                    if self._api_ok(response.status, response_data):
                        nickname = self._find_member_nickname(
                            response_data.get("Data"),
                            member_wxid,
                        )
                        if nickname:
                            return nickname
                        logger.warning(
                            f"在群 {group_id} 中未找到成员 {member_wxid} 的昵称",
                        )
                    else:
                        logger.error(
                            f"获取群成员详情失败: {response.status}, {response_data}",
                        )
                    return None
            except aiohttp.ClientConnectorError as e:
                logger.error(f"连接到 WeChatPadPro 服务失败: {e}")
                return None
            except Exception as e:
                logger.error(f"获取群成员详情时发生错误: {e}")
                return None

    async def _download_raw_image(
        self,
        from_user_name: str,
        to_user_name: str,
        msg_id: int,
    ) -> dict | None:
        """下载原始图片。"""
        url = f"{self.base_url}/Tools/DownloadImg"
        params = self._user_params()
        chat = to_user_name if "@chatroom" in (to_user_name or "") else from_user_name
        payload = {
            "compress_type": 0,
            "to_wxid": chat or to_user_name or from_user_name,
            "msg_id": msg_id,
            "data_len": 0,
            "section": {"start_pos": 0, "data_len": 65536},
        }
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, params=params, json=payload) as response:
                    if response.status == 200:
                        return await response.json()
                    logger.error(f"下载图片失败: {response.status}")
                    return None
            except aiohttp.ClientConnectorError as e:
                logger.error(f"连接到 WeChatPadPro 服务失败: {e}")
                return None
            except Exception as e:
                logger.error(f"下载图片时发生错误: {e}")
                return None

    async def download_voice(
        self,
        to_user_name: str,
        new_msg_id: str,
        bufid: str,
        length: int,
    ):
        """下载原始音频。"""
        url = f"{self.base_url}/Tools/DownloadVoice"
        params = self._user_params()
        payload = {
            "bufid": bufid,
            "fromUserName": to_user_name,
            "msgId": int(new_msg_id) if str(new_msg_id).isdigit() else 0,
            "length": length,
        }
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, params=params, json=payload) as response:
                    if response.status == 200:
                        return await response.json()
                    logger.error(f"下载音频失败: {response.status}")
                    return None
            except aiohttp.ClientConnectorError as e:
                logger.error(f"连接到 WeChatPadPro 服务失败: {e}")
                return None
            except Exception as e:
                logger.error(f"下载音频时发生错误: {e}")
                return None

    async def _process_message_content(
        self,
        abm: AstrBotMessage,
        raw_message: dict,
        msg_type: int,
        content: str,
    ):
        """根据消息类型处理消息内容，填充 AstrBotMessage 的 message 列表。"""
        if msg_type == 1:  # 文本消息
            abm.message_str = content
            if abm.type == MessageType.GROUP_MESSAGE:
                parts = content.split(":\n", 1)
                if len(parts) == 2:
                    message_content = parts[1]
                    abm.message_str = message_content

                    # 检查是否@了机器人，参考 gewechat 的实现方式
                    # 微信大部分客户端在@用户昵称后面，紧接着是一个\u2005字符（四分之一空格）
                    at_me = False

                    # 检查 msg_source 中是否包含机器人的 wxid
                    # wechatpadpro 的格式: <atuserlist>wxid</atuserlist>
                    # gewechat 的格式: <atuserlist><![CDATA[wxid]]></atuserlist>
                    msg_source = raw_message.get("msg_source", "")
                    if (
                        f"<atuserlist>{abm.self_id}</atuserlist>" in msg_source
                        or f"<atuserlist>{abm.self_id}," in msg_source
                        or f",{abm.self_id}</atuserlist>" in msg_source
                    ):
                        at_me = True

                    # 也检查 push_content 中是否有@提示
                    push_content = raw_message.get("push_content", "")
                    if "在群聊中@了你" in push_content:
                        at_me = True

                    if at_me:
                        # 被@了，在消息开头插入At组件（参考gewechat的做法）
                        bot_nickname = await self._get_group_member_nickname(
                            abm.group_id,
                            abm.self_id,
                        )
                        abm.message.insert(
                            0,
                            At(qq=abm.self_id, name=bot_nickname or abm.self_id),
                        )

                        # 只有当消息内容不仅仅是@时才添加Plain组件
                        if "\u2005" in message_content:
                            # 检查@之后是否还有其他内容
                            parts = message_content.split("\u2005")
                            if len(parts) > 1 and any(
                                part.strip() for part in parts[1:]
                            ):
                                abm.message.append(Plain(message_content))
                        else:
                            # 检查是否只包含@机器人
                            is_pure_at = False
                            if (
                                bot_nickname
                                and message_content.strip() == f"@{bot_nickname}"
                            ):
                                is_pure_at = True
                            if not is_pure_at:
                                abm.message.append(Plain(message_content))
                    else:
                        # 没有@机器人，作为普通文本处理
                        abm.message.append(Plain(message_content))
                else:
                    abm.message.append(Plain(abm.message_str))
            else:  # 私聊消息
                abm.message.append(Plain(abm.message_str))

            # 缓存文本消息，以便引用消息可以查找
            try:
                # 获取msg_id作为缓存的key
                new_msg_id = raw_message.get("new_msg_id")
                if new_msg_id:
                    # 限制缓存大小
                    if (
                        len(self.cached_texts) >= self.max_text_cache
                        and self.cached_texts
                    ):
                        # 删除最早的一条缓存
                        oldest_key = next(iter(self.cached_texts))
                        self.cached_texts.pop(oldest_key)

                    logger.debug(f"缓存文本消息，new_msg_id={new_msg_id}")
                    self.cached_texts[str(new_msg_id)] = content
            except Exception as e:
                logger.error(f"缓存文本消息失败: {e}")
        elif msg_type == 3:
            # 图片消息
            from_user_name = raw_message.get("from_user_name", {}).get("str", "")
            to_user_name = raw_message.get("to_user_name", {}).get("str", "")
            msg_id = cast(int, raw_message.get("msg_id"))
            image_resp = await self._download_raw_image(
                from_user_name,
                to_user_name,
                msg_id,
            )
            if image_resp is None:
                logger.error(f"下载图片失败: msg_id={msg_id}")
                return
            image_bs64_data = (
                image_resp.get("Data", {}).get("Data", {}).get("Buffer", None)
            )
            if image_bs64_data:
                abm.message.append(Image.fromBase64(image_bs64_data))
                # 缓存图片，以便引用消息可以查找
                try:
                    # 获取msg_id作为缓存的key
                    new_msg_id = raw_message.get("new_msg_id")
                    if new_msg_id:
                        # 限制缓存大小
                        if (
                            len(self.cached_images) >= self.max_image_cache
                            and self.cached_images
                        ):
                            # 删除最早的一条缓存
                            oldest_key = next(iter(self.cached_images))
                            self.cached_images.pop(oldest_key)

                        logger.debug(f"缓存图片消息，new_msg_id={new_msg_id}")
                        self.cached_images[str(new_msg_id)] = image_bs64_data
                except Exception as e:
                    logger.error(f"缓存图片消息失败: {e}")
        elif msg_type == 47:
            # 视频消息 (注意：表情消息也是 47，需要区分)
            data_parser = GeweDataParser(
                content=content,
                is_private_chat=(abm.type != MessageType.GROUP_MESSAGE),
                raw_message=raw_message,
            )
            emoji_message = data_parser.parse_emoji()
            if emoji_message is not None:
                abm.message.append(emoji_message)
        elif msg_type == 50:
            logger.warning("收到语音/视频消息，待实现。")
        elif msg_type == 34:
            # 语音消息
            bufid = 0
            to_user_name = raw_message.get("to_user_name", {}).get("str", "")
            new_msg_id = raw_message.get("new_msg_id")
            if new_msg_id is None:
                logger.error("语音消息缺少 new_msg_id")
                return
            data_parser = GeweDataParser(
                content=content,
                is_private_chat=(abm.type != MessageType.GROUP_MESSAGE),
                raw_message=raw_message,
            )

            voicemsg = data_parser._format_to_xml().find("voicemsg")
            if voicemsg is None:
                logger.error("无法从 XML 解析 voicemsg 节点")
                return
            bufid = voicemsg.get("bufid") or "0"
            length = int(voicemsg.get("length") or 0)
            voice_resp = await self.download_voice(
                to_user_name=to_user_name,
                new_msg_id=new_msg_id,
                bufid=bufid,
                length=length,
            )
            if voice_resp is None:
                logger.error(f"下载语音失败: new_msg_id={new_msg_id}")
                return
            voice_bs64_data = voice_resp.get("Data", {}).get("Base64", None)
            if voice_bs64_data:
                voice_bytes = base64.b64decode(voice_bs64_data)
                temp_dir = Path(get_astrbot_temp_path())
                temp_dir.mkdir(parents=True, exist_ok=True)
                file_path = temp_dir / f"wechatpadpro_voice_{abm.message_id}.silk"
                file_path.write_bytes(voice_bytes)
                abm.message.append(Record(file=str(file_path), url=str(file_path)))
        elif msg_type == 49:
            try:
                parser = GeweDataParser(
                    content=content,
                    is_private_chat=(abm.type != MessageType.GROUP_MESSAGE),
                    cached_texts=self.cached_texts,
                    cached_images=self.cached_images,
                    raw_message=raw_message,
                    downloader=self._download_raw_image,
                )
                components = await parser.parse_mutil_49()
                if components:
                    abm.message.extend(components)
                    abm.message_str = "\n".join(
                        c.text for c in components if isinstance(c, Plain)
                    )
            except Exception as e:
                logger.warning(f"msg_type 49 处理失败: {e}")
                abm.message.append(Plain("[XML 消息处理失败]"))
                abm.message_str = "[XML 消息处理失败]"
        else:
            logger.warning(f"收到未处理的消息类型: {msg_type}。")

    async def terminate(self):
        """终止一个平台的运行实例。"""
        logger.info("终止 WeChatPadPro 适配器。")
        try:
            if self.ws_handle_task:
                self.ws_handle_task.cancel()
            if self._shutdown_event is not None:
                self._shutdown_event.set()
        except Exception:
            pass

    def meta(self) -> PlatformMetadata:
        """得到一个平台的元数据。"""
        return self.metadata

    async def send_by_session(
        self,
        session: MessageSesion,
        message_chain: MessageChain,
    ):
        dummy_message_obj = AstrBotMessage()
        dummy_message_obj.session_id = session.session_id
        # 根据 session_id 判断消息类型
        if "@chatroom" in session.session_id:
            dummy_message_obj.type = MessageType.GROUP_MESSAGE
            if "#" in session.session_id:
                dummy_message_obj.group_id = session.session_id.split("#")[0]
            else:
                dummy_message_obj.group_id = session.session_id
            dummy_message_obj.sender = MessageMember(user_id="", nickname="")
        else:
            dummy_message_obj.type = MessageType.FRIEND_MESSAGE
            dummy_message_obj.group_id = ""
            dummy_message_obj.sender = MessageMember(user_id="", nickname="")
        sending_event = WeChatPadProMessageEvent(
            message_str="",
            message_obj=dummy_message_obj,
            platform_meta=self.meta(),
            session_id=session.session_id,
            adapter=self,
        )
        await sending_event.send(message_chain)

    async def get_contact_list(self):
        """获取联系人列表。"""
        url = f"{self.base_url}/Friend/GetContractList"
        params = self._user_params()
        payload = {"currentChatRoomContactSeq": 0, "currentWxcontactSeq": 0}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, params=params, json=payload) as response:
                    if response.status != 200:
                        logger.error(f"获取联系人列表失败: {response.status}")
                        return None
                    result = await response.json()
                    if self._api_ok(response.status, result) and result.get("Data"):
                        data = result.get("Data") or {}
                        contact_list = (
                            data.get("ContactList", {}).get("contactUsernameList")
                            if isinstance(data.get("ContactList"), dict)
                            else None
                        )
                        if contact_list is None and isinstance(
                            data.get("contacts"), list
                        ):
                            contact_list = [
                                item.get("wxid")
                                for item in data["contacts"]
                                if isinstance(item, dict) and item.get("wxid")
                            ]
                        return contact_list or []
                    logger.error(f"获取联系人列表失败: {result}")
                    return None
            except aiohttp.ClientConnectorError as e:
                logger.error(f"连接到 WeChatPadPro 服务失败: {e}")
                return None
            except Exception as e:
                logger.error(f"获取联系人列表时发生错误: {e}")
                return None

    async def get_contact_details_list(
        self,
        room_wx_id_list: list[str] | None = None,
        user_names: list[str] | None = None,
    ) -> dict | None:
        """获取联系人详情列表。"""
        if room_wx_id_list is None:
            room_wx_id_list = []
        if user_names is None:
            user_names = []
        url = f"{self.base_url}/Friend/GetContractDetail"
        params = self._user_params()
        names = [*user_names, *room_wx_id_list]
        payload = {"userName": ",".join(names[:20])}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, params=params, json=payload) as response:
                    if response.status != 200:
                        logger.error(f"获取联系人详情列表失败: {response.status}")
                        return None
                    result = await response.json()
                    if self._api_ok(response.status, result) and result.get("Data"):
                        contact_list = result.get("Data", {}).get("contactList", {})
                        return contact_list
                    logger.error(f"获取联系人详情列表失败: {result}")
                    return None
            except aiohttp.ClientConnectorError as e:
                logger.error(f"连接到 WeChatPadPro 服务失败: {e}")
                return None
            except Exception as e:
                logger.error(f"获取联系人详情列表时发生错误: {e}")
                return None
