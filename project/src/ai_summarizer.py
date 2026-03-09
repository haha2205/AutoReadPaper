"""
ai_summarizer.py — AI 智能总结模块

使用 DeepSeek API（兼容 OpenAI 接口）对论文进行中文摘要生成，
支持批量处理和失败重试。
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional

from loguru import logger
from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from .paper_search import Paper


class AISummarizer:
    """
    基于 DeepSeek 的论文 AI 总结器。

    使用 OpenAI 兼容接口，可轻松切换到其他模型提供商
    （只需修改 base_url 和 api_key）。
    """

    def __init__(self, config: dict):
        summary_cfg = config.get("summarizer", {})
        self.batch_size: int = summary_cfg.get("batch_size", 5)
        self.max_tokens: int = summary_cfg.get("max_tokens", 500)
        self.temperature: float = summary_cfg.get("temperature", 0.3)
        self.prompt_template: str = summary_cfg.get("prompt_template", self._default_prompt())

        self._client = AsyncOpenAI(
            api_key=os.getenv("DEEPSEEK_API_KEY"),
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
        )
        self.model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

        if not os.getenv("DEEPSEEK_API_KEY"):
            logger.warning("DEEPSEEK_API_KEY not set — AI summarization will be skipped.")

    @staticmethod
    def _default_prompt() -> str:
        return (
            "你是一名资深科研助手，擅长快速提取论文的核心价值。\n"
            "请用**250字以内的中文**总结以下论文，包含三个部分：\n"
            "1. **核心问题**：这篇论文解决了什么问题？\n"
            "2. **主要方法**：使用了什么关键技术或方法？\n"
            "3. **主要贡献**：最重要的结论或创新点是什么？\n\n"
            "论文标题：{title}\n"
            "论文摘要：{abstract}"
        )

    async def summarize_papers(self, papers: list[Paper]) -> list[Paper]:
        """
        对论文列表进行批量 AI 总结，结果写入 paper.ai_summary 字段。

        Args:
            papers: 待总结的论文列表（abstract 为空的论文会跳过）

        Returns:
            已填充 ai_summary 字段的论文列表
        """
        if not os.getenv("DEEPSEEK_API_KEY"):
            logger.warning("Skipping AI summarization: no API key.")
            return papers

        # 拆分批次并发处理
        batches = [
            papers[i : i + self.batch_size]
            for i in range(0, len(papers), self.batch_size)
        ]

        results: list[Paper] = []
        for batch_idx, batch in enumerate(batches):
            logger.info(
                f"Summarizing batch {batch_idx + 1}/{len(batches)} "
                f"({len(batch)} papers)..."
            )
            batch_results = await asyncio.gather(
                *[self._summarize_one(paper) for paper in batch],
                return_exceptions=True,
            )
            for paper, result in zip(batch, batch_results):
                if isinstance(result, Exception):
                    logger.error(f"Summary failed for '{paper.title[:60]}': {result}")
                    paper.ai_summary = "（AI 总结生成失败，请查看原文摘要）"
                else:
                    paper.ai_summary = result
                results.append(paper)

            # 批次间间隔，避免 API 速率限制
            if batch_idx < len(batches) - 1:
                await asyncio.sleep(1.0)

        return results

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=30))
    async def _summarize_one(self, paper: Paper) -> str:
        """对单篇论文生成 AI 摘要，带自动重试。"""
        abstract = paper.abstract.strip()
        if not abstract:
            return "（无摘要，无法生成 AI 总结）"

        # 截断过长的摘要，节省 Token
        abstract_truncated = abstract[:2000] if len(abstract) > 2000 else abstract

        prompt = self.prompt_template.format(
            title=paper.title,
            abstract=abstract_truncated,
        )

        response = await self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )

        summary = response.choices[0].message.content.strip()
        logger.debug(f"Summary generated for: '{paper.title[:60]}'")
        return summary

    async def summarize_single(self, title: str, abstract: str) -> str:
        """
        对单篇论文（通过标题和摘要）生成 AI 总结，供 API 端点直接调用。
        """
        if not os.getenv("DEEPSEEK_API_KEY"):
            return "（未配置 DeepSeek API Key）"

        paper = Paper(
            paper_id="temp",
            title=title,
            abstract=abstract,
            authors=[],
            published_date="",
            url="",
            source="",
        )
        return await self._summarize_one(paper)
