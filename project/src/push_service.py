"""
push_service.py — 多渠道推送模块

支持渠道:
  - Email (SMTP / aiosmtplib)
  - 企业微信群机器人 (Webhook)
  - 飞书群机器人 (Webhook)
  - Telegram Bot
  - GitHub Issues
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Optional

import aiohttp
import aiosmtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from loguru import logger

from .paper_search import Paper


# ─────────────────────────────────────────────────────────────
# 消息格式化工具
# ─────────────────────────────────────────────────────────────

def format_paper_markdown(paper: Paper, template: str | None = None) -> str:
    """将 Paper 对象格式化为 Markdown 消息字符串。"""
    if template:
        return template.format(
            title=paper.title,
            source=paper.source,
            published_date=paper.published_date,
            url=paper.url,
            authors=", ".join(paper.authors[:3]) + ("..." if len(paper.authors) > 3 else ""),
            ai_summary=paper.ai_summary or paper.abstract[:200],
        )

    authors_str = ", ".join(paper.authors[:3])
    if len(paper.authors) > 3:
        authors_str += " et al."

    return (
        f"## 📄 {paper.title}\n"
        f"**来源**: {paper.source} | **日期**: {paper.published_date}\n"
        f"**链接**: {paper.url}\n"
        f"**作者**: {authors_str}\n\n"
        f"**🤖 AI 速览**:\n{paper.ai_summary or paper.abstract[:300]}\n\n---\n"
    )


def format_digest(papers: list[Paper], template: str | None = None) -> str:
    """将多篇论文合并为一条摘要消息。"""
    today = date.today().strftime("%Y年%m月%d日")
    header = f"# 📚 论文速递 — {today}\n共发现 **{len(papers)}** 篇新论文\n\n"
    body = "\n".join(format_paper_markdown(p, template) for p in papers)
    return header + body


# ─────────────────────────────────────────────────────────────
# 推送渠道基类
# ─────────────────────────────────────────────────────────────

class BasePushChannel:
    async def send(self, papers: list[Paper], message_template: str | None = None) -> bool:
        raise NotImplementedError


# ─────────────────────────────────────────────────────────────
# Email 推送 (SMTP)
# ─────────────────────────────────────────────────────────────

class EmailPushChannel(BasePushChannel):
    def __init__(self):
        self.host = os.getenv("EMAIL_SMTP_HOST", "")
        self.port = int(os.getenv("EMAIL_SMTP_PORT", "465"))
        self.user = os.getenv("EMAIL_SMTP_USER", "")
        self.password = os.getenv("EMAIL_SMTP_PASSWORD", "")
        self.from_addr = os.getenv("EMAIL_FROM", self.user)
        self.to_addrs = [
            addr.strip()
            for addr in os.getenv("EMAIL_TO", "").split(",")
            if addr.strip()
        ]

    async def send(self, papers: list[Paper], message_template: str | None = None) -> bool:
        if not self.to_addrs or not self.host:
            logger.warning("Email push skipped: missing SMTP config.")
            return False

        today = date.today().strftime("%Y-%m-%d")
        subject = f"📚 论文速递 {today} — 共 {len(papers)} 篇"
        html_body = self._build_html(papers, message_template)

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.from_addr
        msg["To"] = ", ".join(self.to_addrs)
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        try:
            await aiosmtplib.send(
                msg,
                hostname=self.host,
                port=self.port,
                username=self.user,
                password=self.password,
                use_tls=self.port == 465,
                start_tls=self.port == 587,
            )
            logger.info(f"Email sent to {self.to_addrs} ({len(papers)} papers).")
            return True
        except Exception as e:
            logger.error(f"Email send failed: {e}")
            return False

    def _build_html(self, papers: list[Paper], template: str | None) -> str:
        today = date.today().strftime("%Y年%m月%d日")
        items_html = ""
        for paper in papers:
            authors = ", ".join(paper.authors[:3])
            summary = paper.ai_summary or paper.abstract[:300]
            items_html += f"""
            <div style="border:1px solid #e0e0e0;border-radius:8px;padding:16px;margin:12px 0;">
              <h3 style="margin:0 0 8px;color:#1a73e8">
                <a href="{paper.url}" style="text-decoration:none">{paper.title}</a>
              </h3>
              <p style="margin:4px 0;color:#666;font-size:13px">
                📅 {paper.published_date} &nbsp;|&nbsp; 📰 {paper.source} &nbsp;|&nbsp; 👤 {authors}
              </p>
              <p style="margin:8px 0;font-size:14px;color:#333">{summary}</p>
            </div>"""

        return f"""
        <html><body style="font-family:Arial,sans-serif;max-width:800px;margin:auto;padding:20px">
          <h1 style="color:#1a73e8">📚 论文速递 — {today}</h1>
          <p>共发现 <strong>{len(papers)}</strong> 篇新论文：</p>
          {items_html}
          <hr><p style="color:#999;font-size:12px">由 AutoReadPaper 自动生成</p>
        </body></html>"""


# ─────────────────────────────────────────────────────────────
# 企业微信群机器人
# ─────────────────────────────────────────────────────────────

class WeComPushChannel(BasePushChannel):
    def __init__(self):
        self.webhook_url = os.getenv("WECOM_WEBHOOK_URL", "")

    async def send(self, papers: list[Paper], message_template: str | None = None) -> bool:
        if not self.webhook_url:
            logger.warning("WeCom push skipped: WECOM_WEBHOOK_URL not set.")
            return False

        # 企业微信 Markdown 消息有 4096 字符限制，分批发送
        chunks = self._split_papers(papers, max_chars=3500)
        success = True

        async with aiohttp.ClientSession() as session:
            for chunk_idx, chunk in enumerate(chunks):
                content = self._build_markdown(chunk, chunk_idx + 1, len(chunks))
                payload = {"msgtype": "markdown", "markdown": {"content": content}}

                try:
                    async with session.post(self.webhook_url, json=payload) as resp:
                        result = await resp.json()
                        if result.get("errcode") != 0:
                            logger.error(f"WeCom send error: {result}")
                            success = False
                        else:
                            logger.info(f"WeCom chunk {chunk_idx+1}/{len(chunks)} sent.")
                except Exception as e:
                    logger.error(f"WeCom push failed: {e}")
                    success = False

        return success

    def _build_markdown(self, papers: list[Paper], chunk: int, total: int) -> str:
        today = date.today().strftime("%Y年%m月%d日")
        suffix = f"（{chunk}/{total}）" if total > 1 else ""
        lines = [f"## 📚 论文速递 {today}{suffix}\n"]
        for paper in papers:
            summary = (paper.ai_summary or paper.abstract[:150]).replace("\n", " ")
            lines.append(
                f"**[{paper.title[:60]}]({paper.url})**\n"
                f"> 📰 {paper.source} | 📅 {paper.published_date}\n\n"
                f"{summary}\n\n---"
            )
        return "\n".join(lines)

    def _split_papers(self, papers: list[Paper], max_chars: int) -> list[list[Paper]]:
        chunks, current, current_len = [], [], 0
        for paper in papers:
            size = len(paper.title) + len(paper.ai_summary or paper.abstract[:150]) + 100
            if current and current_len + size > max_chars:
                chunks.append(current)
                current, current_len = [], 0
            current.append(paper)
            current_len += size
        if current:
            chunks.append(current)
        return chunks


# ─────────────────────────────────────────────────────────────
# 飞书群机器人
# ─────────────────────────────────────────────────────────────

class FeishuPushChannel(BasePushChannel):
    def __init__(self):
        self.webhook_url = os.getenv("FEISHU_WEBHOOK_URL", "")

    async def send(self, papers: list[Paper], message_template: str | None = None) -> bool:
        if not self.webhook_url:
            logger.warning("Feishu push skipped: FEISHU_WEBHOOK_URL not set.")
            return False

        today = date.today().strftime("%Y年%m月%d日")
        # 飞书支持富文本卡片消息
        elements = []
        for paper in papers:
            summary = (paper.ai_summary or paper.abstract[:150]).replace("\n", " ")
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**[{paper.title[:80]}]({paper.url})**\n"
                        f"📰 {paper.source} | 📅 {paper.published_date}\n"
                        f"{summary}"
                    ),
                }
            })
            elements.append({"tag": "hr"})

        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": f"📚 论文速递 — {today}"},
                    "template": "blue",
                },
                "elements": elements,
            },
        }

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(self.webhook_url, json=payload) as resp:
                    result = await resp.json()
                    if result.get("StatusCode") != 0:
                        logger.error(f"Feishu send error: {result}")
                        return False
                    logger.info(f"Feishu push sent ({len(papers)} papers).")
                    return True
            except Exception as e:
                logger.error(f"Feishu push failed: {e}")
                return False


# ─────────────────────────────────────────────────────────────
# Telegram Bot
# ─────────────────────────────────────────────────────────────

class TelegramPushChannel(BasePushChannel):
    BASE_URL = "https://api.telegram.org"

    def __init__(self):
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    async def send(self, papers: list[Paper], message_template: str | None = None) -> bool:
        if not self.bot_token or not self.chat_id:
            logger.warning("Telegram push skipped: missing BOT_TOKEN or CHAT_ID.")
            return False

        url = f"{self.BASE_URL}/bot{self.bot_token}/sendMessage"
        today = date.today().strftime("%Y年%m月%d日")
        success = True

        async with aiohttp.ClientSession() as session:
            # Telegram 单条消息 4096 字符限制，逐条发送论文
            header = f"📚 *论文速递 — {today}*\n共发现 *{len(papers)}* 篇新论文\n\n"
            await self._send_message(session, url, header)

            for paper in papers:
                summary = (paper.ai_summary or paper.abstract[:200]).replace("\n", " ")
                authors = ", ".join(paper.authors[:3])
                text = (
                    f"📄 *{self._escape_md(paper.title[:100])}*\n"
                    f"📰 {paper.source} | 📅 {paper.published_date}\n"
                    f"👤 {authors}\n"
                    f"🔗 [原文链接]({paper.url})\n\n"
                    f"{self._escape_md(summary)}"
                )
                if not await self._send_message(session, url, text):
                    success = False
        return success

    async def _send_message(self, session: aiohttp.ClientSession, url: str, text: str) -> bool:
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": True,
        }
        try:
            async with session.post(url, json=payload) as resp:
                result = await resp.json()
                if not result.get("ok"):
                    logger.error(f"Telegram send error: {result}")
                    return False
                return True
        except Exception as e:
            logger.error(f"Telegram push failed: {e}")
            return False

    @staticmethod
    def _escape_md(text: str) -> str:
        """转义 Telegram MarkdownV2 特殊字符。"""
        specials = r"\_*[]()~`>#+-=|{}.!"
        return "".join(f"\\{c}" if c in specials else c for c in text)


# ─────────────────────────────────────────────────────────────
# GitHub Issues 归档
# ─────────────────────────────────────────────────────────────

class GitHubIssuePushChannel(BasePushChannel):
    BASE_URL = "https://api.github.com"

    def __init__(self):
        self.token = os.getenv("GITHUB_TOKEN", "")
        self.owner = os.getenv("GITHUB_REPO_OWNER", "")
        self.repo = os.getenv("GITHUB_REPO_NAME", "")

    async def send(self, papers: list[Paper], message_template: str | None = None) -> bool:
        if not all([self.token, self.owner, self.repo]):
            logger.warning("GitHub Issues push skipped: missing GITHUB_TOKEN/OWNER/REPO.")
            return False

        today = date.today().strftime("%Y-%m-%d")
        title = f"📚 论文速递 — {today}（共 {len(papers)} 篇）"
        body = format_digest(papers, message_template)

        headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        url = f"{self.BASE_URL}/repos/{self.owner}/{self.repo}/issues"

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    url,
                    headers=headers,
                    json={"title": title, "body": body, "labels": ["paper-digest"]},
                ) as resp:
                    if resp.status == 201:
                        issue = await resp.json()
                        logger.info(f"GitHub Issue created: {issue.get('html_url')}")
                        return True
                    else:
                        error = await resp.text()
                        logger.error(f"GitHub Issue creation failed ({resp.status}): {error}")
                        return False
            except Exception as e:
                logger.error(f"GitHub push failed: {e}")
                return False


# ─────────────────────────────────────────────────────────────
# 推送管理器（统一入口）
# ─────────────────────────────────────────────────────────────

class PushManager:
    """
    统一管理所有推送渠道，根据 config.yaml 的 push.channels 配置
    决定启用哪些渠道，并并发发送。
    """

    def __init__(self, config: dict):
        push_cfg = config.get("push", {})
        channels_cfg = push_cfg.get("channels", {})
        self.max_papers = push_cfg.get("max_papers_per_push", 20)
        self.message_template = push_cfg.get("message_template")

        self.channels: list[BasePushChannel] = []
        if channels_cfg.get("email"):
            self.channels.append(EmailPushChannel())
        if channels_cfg.get("wecom"):
            self.channels.append(WeComPushChannel())
        if channels_cfg.get("feishu"):
            self.channels.append(FeishuPushChannel())
        if channels_cfg.get("telegram"):
            self.channels.append(TelegramPushChannel())
        if channels_cfg.get("github_issue"):
            self.channels.append(GitHubIssuePushChannel())

        logger.info(f"PushManager initialized with {len(self.channels)} active channel(s).")

    async def push(self, papers: list[Paper]) -> dict:
        """
        向所有启用的渠道推送论文，并发执行。

        Returns:
            各渠道发送结果 {channel_name: success_bool}
        """
        if not papers:
            logger.info("No papers to push.")
            return {}

        # 超出上限时只推送最新的
        send_papers = papers[: self.max_papers]
        if len(papers) > self.max_papers:
            logger.warning(
                f"Capped push to {self.max_papers}/{len(papers)} papers."
            )

        import asyncio
        results = await asyncio.gather(
            *[ch.send(send_papers, self.message_template) for ch in self.channels],
            return_exceptions=True,
        )

        outcome = {}
        for channel, result in zip(self.channels, results):
            name = type(channel).__name__
            if isinstance(result, Exception):
                logger.error(f"{name} failed with exception: {result}")
                outcome[name] = False
            else:
                outcome[name] = result

        return outcome
