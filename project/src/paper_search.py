"""
paper_search.py — 多源论文搜索模块

支持来源:
  - arXiv      (通过 arxiv 官方 Python 库)
  - PubMed     (通过 NCBI E-utils REST API)
  - Semantic Scholar (通过官方 API)
  - bioRxiv/medRxiv  (通过 bioRxiv REST API)
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
import arxiv
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential


# ─────────────────────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────────────────────

@dataclass
class Paper:
    """统一的论文数据结构，聚合各来源字段。"""
    paper_id: str                    # 唯一标识（来源前缀 + 原始 ID）
    title: str
    abstract: str
    authors: list[str]
    published_date: str              # ISO 8601 格式
    url: str
    source: str                      # arxiv / pubmed / semantic_scholar / biorxiv
    doi: Optional[str] = None
    categories: list[str] = field(default_factory=list)
    citation_count: Optional[int] = None
    ai_summary: Optional[str] = None # 由 AI 总结模块填充


# ─────────────────────────────────────────────────────────────
# arXiv 搜索
# ─────────────────────────────────────────────────────────────

class ArxivSearcher:
    """基于官方 arxiv 库的异步封装搜索器。"""

    def __init__(self, request_delay: float = 1.5):
        self.request_delay = request_delay
        self.client = arxiv.Client(
            page_size=100,
            delay_seconds=request_delay,
            num_retries=3,
        )

    async def search(
        self,
        query: str,
        max_results: int = 10,
        categories: list[str] | None = None,
    ) -> list[Paper]:
        """
        搜索 arXiv 论文。

        Args:
            query:       搜索关键词
            max_results: 最大返回数量
            categories:  arXiv 分类限制，如 ["cs.AI", "cs.LG"]

        Returns:
            Paper 对象列表
        """
        if categories:
            # 将分类过滤拼接为 arXiv 查询语法
            cat_filter = " OR ".join(f"cat:{c}" for c in categories)
            full_query = f"({query}) AND ({cat_filter})"
        else:
            full_query = query

        # arxiv 库是同步的，通过 run_in_executor 包装为异步
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(
            None, self._sync_search, full_query, max_results
        )
        return results

    def _sync_search(self, query: str, max_results: int) -> list[Paper]:
        search = arxiv.Search(
            query=query,
            max_results=max_results,
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Descending,
        )
        papers = []
        for result in self.client.results(search):
            papers.append(
                Paper(
                    paper_id=f"arxiv:{result.entry_id.split('/')[-1]}",
                    title=result.title,
                    abstract=result.summary,
                    authors=[a.name for a in result.authors],
                    published_date=result.published.strftime("%Y-%m-%d"),
                    url=result.entry_id,
                    source="arXiv",
                    doi=result.doi,
                    categories=result.categories,
                )
            )
        return papers


# ─────────────────────────────────────────────────────────────
# PubMed 搜索
# ─────────────────────────────────────────────────────────────

class PubMedSearcher:
    """通过 NCBI E-utils REST API 搜索 PubMed。"""

    BASE_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    BASE_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
    BASE_ESUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"

    def __init__(self, api_key: str | None = None, days_back: int = 7):
        self.api_key = api_key or os.getenv("NCBI_API_KEY", "")
        self.days_back = days_back

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def search(self, query: str, max_results: int = 10) -> list[Paper]:
        """
        搜索 PubMed，返回最近 days_back 天内的文章。
        """
        date_from = (datetime.now() - timedelta(days=self.days_back)).strftime("%Y/%m/%d")
        date_to = datetime.now().strftime("%Y/%m/%d")

        esearch_params = {
            "db": "pubmed",
            "term": query,
            "retmax": max_results,
            "retmode": "json",
            "sort": "pub_date",
            "mindate": date_from,
            "maxdate": date_to,
            "datetype": "pdat",
        }
        if self.api_key:
            esearch_params["api_key"] = self.api_key

        async with aiohttp.ClientSession() as session:
            # Step 1: 获取 PMID 列表
            async with session.get(self.BASE_ESEARCH, params=esearch_params) as resp:
                resp.raise_for_status()
                esearch_data = await resp.json()

            pmids = esearch_data.get("esearchresult", {}).get("idlist", [])
            if not pmids:
                return []

            # Step 2: 获取摘要信息
            esummary_params = {
                "db": "pubmed",
                "id": ",".join(pmids),
                "retmode": "json",
            }
            if self.api_key:
                esummary_params["api_key"] = self.api_key

            async with session.get(self.BASE_ESUMMARY, params=esummary_params) as resp:
                resp.raise_for_status()
                summary_data = await resp.json()

        papers = []
        result_map = summary_data.get("result", {})
        for pmid in pmids:
            item = result_map.get(pmid)
            if not item or not isinstance(item, dict):
                continue

            title = item.get("title", "").rstrip(".")
            pub_date = item.get("pubdate", "")[:10] or "Unknown"
            authors = [a.get("name", "") for a in item.get("authors", [])]
            doi = next(
                (
                    aid.get("value", "")
                    for aid in item.get("articleids", [])
                    if aid.get("idtype") == "doi"
                ),
                None,
            )

            papers.append(
                Paper(
                    paper_id=f"pubmed:{pmid}",
                    title=title,
                    abstract="",          # esummary 不含摘要，需 efetch（按需扩展）
                    authors=authors,
                    published_date=pub_date,
                    url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                    source="PubMed",
                    doi=doi,
                )
            )
        return papers


# ─────────────────────────────────────────────────────────────
# Semantic Scholar 搜索
# ─────────────────────────────────────────────────────────────

class SemanticScholarSearcher:
    """通过 Semantic Scholar Graph API 搜索论文。"""

    BASE_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
    FIELDS = "paperId,title,abstract,authors,year,publicationDate,url,externalIds,citationCount,fieldsOfStudy"

    def __init__(self, api_key: str | None = None, require_abstract: bool = True):
        self.api_key = api_key or os.getenv("SEMANTIC_SCHOLAR_API_KEY", "")
        self.require_abstract = require_abstract

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def search(self, query: str, max_results: int = 10) -> list[Paper]:
        headers = {"x-api-key": self.api_key} if self.api_key else {}
        params = {
            "query": query,
            "limit": min(max_results * 2, 100),  # 多取一些以便过滤
            "fields": self.FIELDS,
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(self.BASE_URL, params=params, headers=headers) as resp:
                if resp.status == 429:
                    logger.warning("Semantic Scholar rate limit hit, waiting 30s...")
                    await asyncio.sleep(30)
                    raise Exception("Rate limit")
                resp.raise_for_status()
                data = await resp.json()

        papers = []
        for item in data.get("data", []):
            abstract = item.get("abstract") or ""
            if self.require_abstract and not abstract.strip():
                continue

            pub_date = (
                item.get("publicationDate")
                or (str(item.get("year")) if item.get("year") else "Unknown")
            )
            doi = item.get("externalIds", {}).get("DOI")
            url = (
                item.get("url")
                or f"https://www.semanticscholar.org/paper/{item['paperId']}"
            )

            papers.append(
                Paper(
                    paper_id=f"s2:{item['paperId']}",
                    title=item.get("title", ""),
                    abstract=abstract,
                    authors=[a.get("name", "") for a in item.get("authors", [])],
                    published_date=pub_date[:10] if pub_date and len(pub_date) >= 10 else pub_date,
                    url=url,
                    source="Semantic Scholar",
                    doi=doi,
                    citation_count=item.get("citationCount"),
                    categories=item.get("fieldsOfStudy") or [],
                )
            )
            if len(papers) >= max_results:
                break

        return papers


# ─────────────────────────────────────────────────────────────
# bioRxiv / medRxiv 搜索
# ─────────────────────────────────────────────────────────────

class BioRxivSearcher:
    """通过 bioRxiv/medRxiv Content API 搜索预印本。"""

    BASE_URL = "https://api.biorxiv.org"

    def __init__(self, server: str = "biorxiv", days_back: int = 7):
        self.server = server          # "biorxiv" 或 "medrxiv"
        self.days_back = days_back

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def search(self, query: str, max_results: int = 10) -> list[Paper]:
        date_from = (datetime.now() - timedelta(days=self.days_back)).strftime("%Y-%m-%d")
        date_to = datetime.now().strftime("%Y-%m-%d")
        url = f"{self.BASE_URL}/details/{self.server}/{date_from}/{date_to}/0/json"

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                data = await resp.json()

        query_lower = query.lower()
        papers = []
        for item in data.get("collection", []):
            title = item.get("title", "")
            abstract = item.get("abstract", "")
            # 关键词过滤（bioRxiv API 不支持全文检索，需本地过滤）
            if query_lower not in title.lower() and query_lower not in abstract.lower():
                continue

            doi = item.get("doi", "")
            papers.append(
                Paper(
                    paper_id=f"biorxiv:{doi.replace('/', '_')}",
                    title=title,
                    abstract=abstract,
                    authors=item.get("authors", "").split("; "),
                    published_date=item.get("date", "Unknown"),
                    url=f"https://doi.org/{doi}" if doi else "",
                    source=self.server.capitalize(),
                    doi=doi,
                    categories=[item.get("category", "")],
                )
            )
            if len(papers) >= max_results:
                break

        return papers


# ─────────────────────────────────────────────────────────────
# 统一搜索入口
# ─────────────────────────────────────────────────────────────

class PaperSearchManager:
    """
    统一管理多源搜索，根据 config/keywords.yaml 的配置并发执行搜索，
    并对结果进行基础去重（相同 paper_id 只保留一条）。
    """

    def __init__(self, config: dict):
        search_cfg = config.get("search", {})
        sources_cfg = search_cfg.get("sources", {})

        self.delay = search_cfg.get("request_delay", 1.5)
        self.concurrency = search_cfg.get("concurrency", 3)

        self.searchers: dict[str, object] = {}

        if sources_cfg.get("arxiv", {}).get("enabled", True):
            self.searchers["arxiv"] = ArxivSearcher(request_delay=self.delay)

        if sources_cfg.get("pubmed", {}).get("enabled", True):
            self.searchers["pubmed"] = PubMedSearcher(
                days_back=sources_cfg.get("pubmed", {}).get("days_back", 7)
            )

        if sources_cfg.get("semantic_scholar", {}).get("enabled", True):
            self.searchers["semantic_scholar"] = SemanticScholarSearcher(
                require_abstract=sources_cfg.get("semantic_scholar", {}).get("require_abstract", True)
            )

        if sources_cfg.get("biorxiv", {}).get("enabled", False):
            self.searchers["biorxiv"] = BioRxivSearcher(
                server=sources_cfg.get("biorxiv", {}).get("server", "biorxiv"),
                days_back=sources_cfg.get("biorxiv", {}).get("days_back", 7),
            )

    async def search_all(
        self,
        keywords_config: dict,
        default_max: int = 10,
    ) -> list[Paper]:
        """
        根据 keywords.yaml 的 research_areas 配置并发执行所有搜索任务。

        Returns:
            去重后的 Paper 列表
        """
        semaphore = asyncio.Semaphore(self.concurrency)
        tasks = []

        for area in keywords_config.get("research_areas", []):
            if not area.get("enabled", True):
                continue
            max_results = area.get("max_results", default_max)
            area_sources = area.get("sources", list(self.searchers.keys()))
            arxiv_cats = area.get("arxiv_categories", [])

            for keyword in area.get("keywords", []):
                for source_name in area_sources:
                    searcher = self.searchers.get(source_name)
                    if not searcher:
                        continue
                    tasks.append(
                        self._search_with_semaphore(
                            semaphore, searcher, keyword, max_results, arxiv_cats, area["name"]
                        )
                    )

        results_nested = await asyncio.gather(*tasks, return_exceptions=True)

        all_papers: list[Paper] = []
        seen_ids: set[str] = set()
        for result in results_nested:
            if isinstance(result, Exception):
                logger.warning(f"Search task failed: {result}")
                continue
            for paper in result:
                if paper.paper_id not in seen_ids:
                    seen_ids.add(paper.paper_id)
                    all_papers.append(paper)

        logger.info(f"Total papers collected (ID-deduped): {len(all_papers)}")
        return all_papers

    async def _search_with_semaphore(
        self,
        semaphore: asyncio.Semaphore,
        searcher,
        keyword: str,
        max_results: int,
        arxiv_cats: list[str],
        area_name: str,
    ) -> list[Paper]:
        async with semaphore:
            try:
                if isinstance(searcher, ArxivSearcher):
                    papers = await searcher.search(keyword, max_results, arxiv_cats or None)
                else:
                    papers = await searcher.search(keyword, max_results)
                await asyncio.sleep(self.delay)
                logger.debug(f"[{area_name}] '{keyword}' → {len(papers)} papers from {type(searcher).__name__}")
                return papers
            except Exception as e:
                logger.error(f"[{area_name}] '{keyword}' search failed: {e}")
                return []
