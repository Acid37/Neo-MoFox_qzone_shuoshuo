"""Cookie 管理器。

负责从本地文件加载、通过适配器获取、以及验证 Cookie 有效性。
Cookie 可能会刷新失效，因此需要定期检查。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiofiles

from src.app.plugin_system.api import adapter_api
from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("qzone_shuoshuo")



class CookieManager:
    """Cookie 管理器"""

    def __init__(self, data_dir: Path) -> None:
        """初始化 Cookie 管理器

        Args:
            data_dir: 数据存储目录
        """
        self.data_dir = data_dir
        self.cookies_dir = self.data_dir / "cookies"
        self.cookies_dir.mkdir(parents=True, exist_ok=True)

    def _get_cookie_path(self, qq: str) -> Path:
        """获取 Cookie 文件路径"""
        return self.cookies_dir / f"cookies-{qq}.json"

    async def load_cookies(self, qq: str) -> dict[str, str] | None:
        """从本地文件加载 Cookie

        Args:
            qq: QQ号

        Returns:
            Cookie 字典或 None（文件不存在、格式损坏或字段不完整时返回 None）
        """
        path = self._get_cookie_path(qq)
        if not path.exists():
            return None

        try:
            async with aiofiles.open(path, "r", encoding="utf-8") as f:
                content = await f.read()
                cookies = json.loads(content)

            if not self._validate_cookies(cookies):
                logger.warning(
                    f"[Cookie校验] QQ:{qq} 本地 Cookie 缺少必需字段(p_skey/uin)，视为无效"
                )
                return None

            return cookies
        except Exception as e:
            logger.error(f"加载 Cookie 失败 {qq}: {e}")
            return None

    async def save_cookies(self, qq: str, cookies: dict[str, str]) -> None:
        """保存 Cookie 到本地文件

        Args:
            qq: QQ号
            cookies: Cookie 字典
        """
        path = self._get_cookie_path(qq)
        try:
            async with aiofiles.open(path, "w", encoding="utf-8") as f:
                content = json.dumps(cookies, ensure_ascii=False, indent=2)
                await f.write(content)
            logger.info(f"Cookie 已保存: {path}")
        except Exception as e:
            logger.error(f"保存 Cookie 失败 {qq}: {e}")

    async def fetch_cookies_from_adapter(self, adapter_sign: str) -> dict[str, str] | None:
        """从适配器获取 Cookie（含退避重试，应对适配器偶发超时/消息丢失）。

        Args:
            adapter_sign: 适配器签名

        Returns:
            Cookie 字典或 None
        """
        max_retries = 3
        last_error: str | None = None

        for attempt in range(max_retries):
            try:
                result = await adapter_api.send_adapter_command(
                    adapter_sign=adapter_sign,
                    command_name="get_cookies",
                    command_data={"domain": "user.qzone.qq.com"},
                    timeout=20.0,
                )

                if result.get("status") == "ok":
                    data = result.get("data", {})
                    cookie_str = data.get("cookies", "")
                    if cookie_str:
                        cookies = self._parse_cookie_str(cookie_str)
                        if attempt > 0:
                            logger.info(f"[Cookie获取] 第 {attempt + 1} 次重试成功")
                        return cookies

                # 区分"等待连接"与"真实错误"
                msg = str(result)
                if "连接未建立" in msg or "WebSocket" in msg:
                    last_error = "适配器连接未建立"
                    logger.warning(f"[Cookie获取] 适配器未连接 (尝试 {attempt + 1}/{max_retries})")
                else:
                    last_error = str(result.get("message", result))
                    logger.warning(
                        f"[Cookie获取] 适配器返回错误 (尝试 {attempt + 1}/{max_retries}): {last_error}"
                    )

            except Exception as e:
                last_error = str(e)
                err_msg = str(e)
                if "连接未建立" in err_msg or "WebSocket" in err_msg:
                    logger.warning(f"[Cookie获取] 适配器未连接 (尝试 {attempt + 1}/{max_retries}): {e}")
                else:
                    logger.error(f"[Cookie获取] 异常 (尝试 {attempt + 1}/{max_retries}): {e}")

            if attempt < max_retries - 1:
                delay = 2 ** attempt
                logger.info(f"[Cookie获取] 等待 {delay}s 后重试...")
                await asyncio.sleep(delay)

        logger.error(f"[Cookie获取] 已重试 {max_retries} 次，全部失败，最后错误: {last_error}")
        return None

    def _parse_cookie_str(self, cookie_str: str) -> dict[str, str]:
        """解析 Cookie 字符串

        Args:
            cookie_str: Cookie 字符串 "k=v; k2=v2"

        Returns:
            Cookie 字典
        """
        cookies: dict[str, str] = {}
        for item in cookie_str.split(";"):
            if "=" in item:
                k, v = item.strip().split("=", 1)
                cookies[k.strip()] = v.strip()
        return cookies

    @staticmethod
    def _validate_cookies(cookies: dict[str, str]) -> bool:
        """校验 Cookie 字典是否包含必需的完整字段。

        必须同时满足：
        - 含有 p_skey 或 skey 字段（用于计算 GTK 签名）且非空
        - 含有 uin 或 ptui_loginuin 字段（用于标识用户）且非空

        Args:
            cookies: Cookie 字典

        Returns:
            True 表示 Cookie 完整性校验通过
        """
        if not isinstance(cookies, dict) or not cookies:
            return False

        has_skey = bool(
            (cookies.get("p_skey") or cookies.get("P_skey") or "").strip()
            or (cookies.get("skey") or cookies.get("Skey") or "").strip()
        )
        has_uin = bool(
            (cookies.get("uin") or cookies.get("ptui_loginuin") or "").strip()
        )

        return has_skey and has_uin

    async def get_cookies(self, qq: str, adapter_sign: str = "") -> dict[str, str] | None:
        """获取 Cookie（优先本地，失败则尝试适配器）

        Args:
            qq: QQ号
            adapter_sign: 适配器签名（如果需要从适配器获取）

        Returns:
            Cookie 字典或 None
        """
        # 1. 尝试本地加载
        cookies = await self.load_cookies(qq)
        if cookies:
            return cookies

        # 2. 尝试从适配器获取（内部已含退避重试）
        if adapter_sign:
            logger.info(f"本地无有效 Cookie，尝试从适配器 {adapter_sign} 获取...")
            cookies = await self.fetch_cookies_from_adapter(adapter_sign)
            if cookies and self._validate_cookies(cookies):
                await self.save_cookies(qq, cookies)
                return cookies
            if cookies:
                logger.warning(
                    "[Cookie获取] 适配器返回的 Cookie 不完整(缺少 p_skey/uin)，已丢弃"
                )

        return None

    async def delete_cookies(self, qq: str) -> bool:
        """删除指定QQ号的Cookie文件

        Args:
            qq: QQ号

        Returns:
            是否删除成功
        """
        path = self._get_cookie_path(qq)
        try:
            if path.exists():
                path.unlink()
                logger.info(f"Cookie 已删除: {path}")
                return True
            return False
        except Exception as e:
            logger.error(f"删除 Cookie 失败 {qq}: {e}")
            return False

    async def refresh_cookies(self, qq: str, adapter_sign: str) -> dict[str, str] | None:
        """强制从适配器刷新 Cookie（用于 Cookie 失效时）

        Args:
            qq: QQ号
            adapter_sign: 适配器签名

        Returns:
            新 Cookie 字典或 None（刷新失败或返回的 Cookie 不完整时返回 None）
        """
        logger.info(f"[Cookie刷新] 正在从适配器刷新 QQ:{qq} 的 Cookie...")
        cookies = await self.fetch_cookies_from_adapter(adapter_sign)
        if not cookies:
            logger.error("[Cookie刷新] 从适配器获取新 Cookie 失败")
            return None

        if not self._validate_cookies(cookies):
            logger.error(
                "[Cookie刷新] 获取到的 Cookie 不完整(缺少 p_skey/uin)，"
                "已丢弃，不写入本地以避免毒缓存"
            )
            return None

        await self.save_cookies(qq, cookies)
        logger.info("[Cookie刷新] 成功获取并保存新 Cookie")
        return cookies