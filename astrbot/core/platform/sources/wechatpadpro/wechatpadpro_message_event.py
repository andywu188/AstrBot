import asyncio
import base64
import io
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp
from PIL import Image as PILImage

from astrbot import logger
from astrbot.core.message.components import (
    At,
    Image,
    Plain,
    Record,
    WechatEmoji,
)
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
from astrbot.core.utils.media_utils import ensure_wav
from astrbot.core.utils.tencent_record_helper import wav_to_tencent_silk

if TYPE_CHECKING:
    from .wechatpadpro_adapter import WeChatPadProAdapter


class WeChatPadProMessageEvent(AstrMessageEvent):
    def __init__(
        self,
        message_str: str,
        message_obj: AstrBotMessage,
        platform_meta: PlatformMetadata,
        session_id: str,
        adapter: "WeChatPadProAdapter",  # 传递适配器实例
    ):
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self.message_obj = message_obj  # Save the full message object
        self.adapter = adapter  # Save the adapter instance

    async def send(self, message: MessageChain):
        target = self.session_id
        if self.get_group_id() and "#" in target:
            target = target.split("#", 1)[0]
        is_group = "@chatroom" in target
        async with self.adapter._client_session() as session:
            text_parts: list[str] = []
            at_ids: list[str] = []

            async def flush_text() -> None:
                if not text_parts and not at_ids:
                    return
                ids = list(at_ids)
                if (
                    is_group
                    and self.adapter.settings.get("reply_with_mention", False)
                    and self.message_obj.sender
                    and self.message_obj.sender.user_id
                    and self.message_obj.sender.user_id not in ids
                ):
                    ids.insert(0, str(self.message_obj.sender.user_id))
                await self._send_text(session, "".join(text_parts), target, ids)
                text_parts.clear()
                at_ids.clear()

            for comp in message.chain:
                if isinstance(comp, Plain):
                    text_parts.append(comp.text)
                    continue
                if isinstance(comp, At):
                    wxid = str(comp.qq)
                    if wxid == "all":
                        logger.warning(
                            "WeChatPadProMAX 的 At 只接受成员 wxid，暂不支持 @所有人。",
                        )
                    elif wxid and wxid not in at_ids:
                        at_ids.append(wxid)
                    text_parts.append(f"@{(comp.name or wxid)}\u2005")
                    continue
                await flush_text()
                await asyncio.sleep(1)
                if isinstance(comp, Image):
                    await self._send_image(session, comp, target)
                elif isinstance(comp, WechatEmoji):
                    await self._send_emoji(session, comp, target)
                elif isinstance(comp, Record):
                    await self._send_voice(session, comp, target)
            await flush_text()
        await super().send(message)

    async def send_streaming(
        self, generator: AsyncGenerator[MessageChain, None], use_fallback: bool = False
    ):
        buffer = None
        async for chain in generator:
            if not buffer:
                buffer = chain
            else:
                buffer.chain.extend(chain.chain)
        if not buffer:
            return None
        buffer.squash_plain()
        await self.send(buffer)
        return await super().send_streaming(generator, use_fallback)

    async def _send_image(
        self, session: aiohttp.ClientSession, comp: Image, target: str
    ):
        b64 = await comp.convert_to_base64()
        raw = self._validate_base64(b64)
        b64c = self._compress_image(raw)
        payload = {"Base64": b64c, "ToWxid": target}
        url = f"{self.adapter.base_url}/Msg/UploadImg"
        await self._post(session, url, payload)

    async def _send_text(
        self,
        session: aiohttp.ClientSession,
        text: str,
        target: str,
        at_ids: list[str] | None = None,
    ):
        payload = {"Content": text, "ToWxid": target, "Type": 1}
        # /Msg/SendTxt At is a comma-separated wxid list. One call can mention several people.
        if at_ids and "@chatroom" in target:
            payload["At"] = ",".join(at_ids)
        url = f"{self.adapter.base_url}/Msg/SendTxt"
        await self._post(session, url, payload)

    async def _send_emoji(
        self, session: aiohttp.ClientSession, comp: WechatEmoji, target: str
    ):
        payload = {"Md5": comp.md5, "ToWxid": target, "TotalLen": comp.md5_len}
        url = f"{self.adapter.base_url}/Msg/SendEmoji"
        await self._post(session, url, payload)

    async def _send_voice(
        self, session: aiohttp.ClientSession, comp: Record, target: str
    ):
        record_path = await comp.convert_to_file_path()
        wav_path = await ensure_wav(record_path)
        temp_dir = Path(get_astrbot_temp_path())
        temp_dir.mkdir(parents=True, exist_ok=True)
        silk_path = temp_dir / f"wechatpadpro_send_{uuid.uuid4().hex}.silk"
        duration = await wav_to_tencent_silk(wav_path, str(silk_path))
        voice_b64 = base64.b64encode(silk_path.read_bytes()).decode()
        payload = {
            "Base64": voice_b64,
            "ToWxid": target,
            "Type": 4,
            "VoiceTime": max(1000, int(duration * 1000)),
        }
        url = f"{self.adapter.base_url}/Msg/SendVoice"
        await self._post(session, url, payload)

    @staticmethod
    def _validate_base64(b64: str) -> bytes:
        return base64.b64decode(b64, validate=True)

    @staticmethod
    def _compress_image(data: bytes) -> str:
        img = PILImage.open(io.BytesIO(data))
        buf = io.BytesIO()
        if img.format == "JPEG":
            img.save(buf, "JPEG", quality=80)
        else:
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.save(buf, "JPEG", quality=80)
        # logger.info("图片处理完成！！！")
        return base64.b64encode(buf.getvalue()).decode()

    async def _post(self, session, url, payload):
        params = {"authcode": self.adapter.auth_key}
        try:
            async with session.post(url, params=params, json=payload) as resp:
                data = await resp.json()
                if not self.adapter._api_ok(resp.status, data):
                    logger.error(f"{url} failed: {resp.status} {data}")
        except Exception as e:
            logger.error(f"{url} error: {e}")


# TODO: 添加对其他消息组件类型的处理 (Record, Video, At等)
# elif isinstance(component, Record):
#     pass
# elif isinstance(component, Video):
#     pass
# elif isinstance(component, At):
#     pass
# ...
