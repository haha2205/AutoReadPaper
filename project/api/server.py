"""
api/server.py — FastAPI HTTP 服务

暴露以下端点供 n8n HTTP Request 节点调用:
  GET  /health              健康检查
  POST /api/search          触发多源论文搜索（含去重）
  POST /api/pipeline/run    run 一次完整流水线（搜索→去重→AI总结→向量存储→推送）
  GET  /api/papers/recent   查询近期已存储论文
  POST /api/search/semantic 语义相似论文检索
  POST /api/summarize       对单篇论文生成 AI 总结
  GET  /api/stats           向量库统计信息
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Optional

import yaml
from fastapi import FastAPI, HTTPException, Query
from loguru import logger
from pydantic import BaseModel, Field

from src.ai_summarizer import AISummarizer
from src.paper_search import Paper, PaperSearchManager
from src.push_service import PushManager
from src.vector_store import VectorStore

# ─────────────────────────────────────────────────────────────
# 配置加载
# ─────────────────────────────────────────────────────────────

CONFIG_PATH = os.getenv("CONFIG_PATH", "config/config.yaml")
KEYWORDS_PATH = os.getenv("KEYWORDS_PATH", "config/keywords.yaml")


@lru_cache(maxsize=1)
def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=1)
def load_keywords() -> dict:
    with open(KEYWORDS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ─────────────────────────────────────────────────────────────
# 应用生命周期 + 依赖注入
# ─────────────────────────────────────────────────────────────

_services: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config()
    _services["searcher"] = PaperSearchManager(cfg)
    _services["summarizer"] = AISummarizer(cfg)
    _services["vector_store"] = VectorStore(cfg)
    _services["push_manager"] = PushManager(cfg)
    logger.info("All services initialized.")
    yield
    _services.clear()


app = FastAPI(
    title="AutoReadPaper API",
    description="论文自动抓取、AI 总结、向量存储与多渠道推送后端服务",
    version="1.0.0",
    lifespan=lifespan,
)


# ─────────────────────────────────────────────────────────────
# Pydantic 请求/响应模型
# ─────────────────────────────────────────────────────────────

class PaperOut(BaseModel):
    paper_id: str
    title: str
    abstract: str
    authors: list[str]
    published_date: str
    url: str
    source: str
    doi: Optional[str] = None
    categories: list[str] = []
    citation_count: Optional[int] = None
    ai_summary: Optional[str] = None


class SearchRequest(BaseModel):
    keywords: Optional[list[str]] = Field(
        default=None,
        description="自定义关键词列表；留空则使用 keywords.yaml 中的配置"
    )
    sources: Optional[list[str]] = Field(
        default=None,
        description="指定来源: arxiv / pubmed / semantic_scholar / biorxiv"
    )
    max_results: int = Field(default=10, ge=1, le=50)


class SummarizeRequest(BaseModel):
    title: str
    abstract: str


class SemanticSearchRequest(BaseModel):
    query: str
    top_k: int = Field(default=10, ge=1, le=50)


class PipelineResult(BaseModel):
    new_papers_count: int
    push_results: dict
    stats: dict


# ─────────────────────────────────────────────────────────────
# 路由
# ─────────────────────────────────────────────────────────────

@app.get("/health", tags=["系统"])
async def health_check():
    return {"status": "ok", "service": "AutoReadPaper API"}


@app.post("/api/search", response_model=list[PaperOut], tags=["论文搜索"])
async def search_papers(req: SearchRequest):
    """
    触发多源论文搜索并返回去重后的新论文列表（不触发 AI 总结和推送）。
    适合在 n8n 中作为独立步骤调用。
    """
    searcher: PaperSearchManager = _services["searcher"]
    vector_store: VectorStore = _services["vector_store"]

    # 如果提供了自定义关键词，构建临时 keywords 配置
    if req.keywords:
        keywords_cfg = {
            "research_areas": [{
                "name": "custom",
                "enabled": True,
                "keywords": req.keywords,
                "sources": req.sources or ["arxiv", "semantic_scholar"],
                "max_results": req.max_results,
            }]
        }
    else:
        keywords_cfg = load_keywords()

    cfg = load_config()
    default_max = cfg.get("search", {}).get("default_max_results", 10)
    all_papers = await searcher.search_all(keywords_cfg, default_max)
    new_papers = vector_store.filter_new_papers(all_papers)

    return [PaperOut(**paper.__dict__) for paper in new_papers]


@app.post("/api/pipeline/run", response_model=PipelineResult, tags=["完整流水线"])
async def run_pipeline():
    """
    执行完整的自动化流水线：
    搜索 → 去重过滤 → AI 总结 → 向量存储 → 多渠道推送

    这是 n8n 定时触发器的核心调用接口。
    """
    searcher: PaperSearchManager = _services["searcher"]
    summarizer: AISummarizer = _services["summarizer"]
    vector_store: VectorStore = _services["vector_store"]
    push_manager: PushManager = _services["push_manager"]

    keywords_cfg = load_keywords()
    cfg = load_config()
    default_max = cfg.get("search", {}).get("default_max_results", 10)

    # Step 1: 多源搜索
    logger.info("Pipeline Step 1: Multi-source search...")
    all_papers = await searcher.search_all(keywords_cfg, default_max)

    # Step 2: 语义去重过滤
    logger.info("Pipeline Step 2: Deduplication...")
    new_papers = vector_store.filter_new_papers(all_papers)

    if not new_papers:
        logger.info("Pipeline: No new papers found. Skipping rest of pipeline.")
        return PipelineResult(
            new_papers_count=0,
            push_results={},
            stats=vector_store.get_stats(),
        )

    # Step 3: AI 总结
    logger.info(f"Pipeline Step 3: AI summarization for {len(new_papers)} papers...")
    summarized_papers = await summarizer.summarize_papers(new_papers)

    # Step 4: 向量存储
    logger.info("Pipeline Step 4: Saving to vector store...")
    saved_count = vector_store.add_papers(summarized_papers)

    # Step 5: 多渠道推送
    logger.info("Pipeline Step 5: Multi-channel push...")
    push_results = await push_manager.push(summarized_papers)

    return PipelineResult(
        new_papers_count=saved_count,
        push_results=push_results,
        stats=vector_store.get_stats(),
    )


@app.post("/api/summarize", tags=["AI 总结"])
async def summarize_paper(req: SummarizeRequest):
    """对单篇论文（标题+摘要）调用 DeepSeek 生成中文摘要。"""
    summarizer: AISummarizer = _services["summarizer"]
    summary = await summarizer.summarize_single(req.title, req.abstract)
    return {"summary": summary}


@app.post("/api/search/semantic", tags=["语义检索"])
async def semantic_search(req: SemanticSearchRequest):
    """在已存储的论文向量库中执行语义相似检索。"""
    vector_store: VectorStore = _services["vector_store"]
    results = vector_store.semantic_search(req.query, req.top_k)
    return {"query": req.query, "results": results}


@app.get("/api/stats", tags=["系统"])
async def get_stats():
    """返回向量库统计信息。"""
    vector_store: VectorStore = _services["vector_store"]
    return vector_store.get_stats()
