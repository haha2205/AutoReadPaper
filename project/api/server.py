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


# ─────────────────────────────────────────────────────────────
# 关键词配置管理 API
# ─────────────────────────────────────────────────────────────

@app.get("/api/keywords", tags=["配置管理"])
async def get_keywords():
    """获取当前的关键词配置。"""
    load_keywords.cache_clear()  # 清除缓存以读取最新配置
    return load_keywords()


@app.post("/api/keywords/reload", tags=["配置管理"])
async def reload_keywords():
    """重新加载关键词配置（无需重启容器）。"""
    load_keywords.cache_clear()
    keywords = load_keywords()
    return {"status": "ok", "message": "Keywords reloaded successfully", "keywords": keywords}


class KeywordUpdate(BaseModel):
    research_areas: list[dict]


@app.post("/api/keywords/update", tags=["配置管理"])
async def update_keywords(update: KeywordUpdate):
    """更新关键词配置文件。"""
    try:
        with open(KEYWORDS_PATH, "w", encoding="utf-8") as f:
            yaml.dump({"research_areas": update.research_areas}, f, allow_unicode=True, default_flow_style=False)
        load_keywords.cache_clear()
        return {"status": "ok", "message": "Keywords updated successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update keywords: {str(e)}")


# ─────────────────────────────────────────────────────────────
# 论文数据查询 API
# ─────────────────────────────────────────────────────────────

@app.get("/api/papers/list", tags=["论文查询"])
async def list_papers(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    source: Optional[str] = Query(None, description="过滤来源: arxiv, pubmed 等")
):
    """列出已存储的论文（支持分页和过滤）。"""
    vector_store: VectorStore = _services["vector_store"]
    try:
        where_filter = {"source": source} if source else None
        # ChromaDB 不支持 offset，需要先获取所有数据再切片
        results = vector_store.collection.get(
            where=where_filter,
            include=["metadatas", "documents"]
        )
        
        # 手动实现分页
        start_idx = offset
        end_idx = offset + limit
        
        papers = []
        for i, (paper_id, metadata, document) in enumerate(zip(
            results["ids"][start_idx:end_idx], 
            results["metadatas"][start_idx:end_idx], 
            results["documents"][start_idx:end_idx]
        )):
            papers.append({
                "id": paper_id,
                "title": metadata.get("title", ""),
                "authors": metadata.get("authors", ""),
                "source": metadata.get("source", ""),
                "published_date": metadata.get("published_date", ""),
                "url": metadata.get("url", ""),
                "categories": metadata.get("categories", ""),
                "citation_count": metadata.get("citation_count", 0),
                "abstract_snippet": document[:200] + "..." if len(document) > 200 else document
            })
        
        total = len(results["ids"])
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "papers": papers
        }
    except Exception as e:
        logger.error(f"Failed to list papers: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to list papers: {str(e)}")


# ─────────────────────────────────────────────────────────────
# Web 管理界面
# ─────────────────────────────────────────────────────────────

from fastapi.responses import HTMLResponse

@app.get("/admin", response_class=HTMLResponse, tags=["管理界面"])
async def admin_panel():
    """简单的 Web 管理界面。"""
    html_content = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AutoReadPaper 管理面板</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }
        .container {
            max-width: 1400px;
            margin: 0 auto;
            background: white;
            border-radius: 12px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            overflow: hidden;
        }
        .header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            text-align: center;
        }
        .header h1 { font-size: 2em; margin-bottom: 10px; }
        .tabs {
            display: flex;
            background: #f8f9fa;
            border-bottom: 2px solid #e9ecef;
        }
        .tab {
            flex: 1;
            padding: 15px;
            text-align: center;
            cursor: pointer;
            transition: all 0.3s;
            font-weight: 500;
        }
        .tab:hover { background: #e9ecef; }
        .tab.active { background: white; border-bottom: 3px solid #667eea; }
        .content { padding: 30px; }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        .stat-card {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 20px;
            border-radius: 8px;
            text-align: center;
        }
        .stat-card h3 { font-size: 2em; margin-bottom: 5px; }
        .stat-card p { opacity: 0.9; }
        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 20px;
        }
        th, td {
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #e9ecef;
        }
        th {
            background: #f8f9fa;
            font-weight: 600;
            position: sticky;
            top: 0;
        }
        tr:hover { background: #f8f9fa; }
        .btn {
            background: #667eea;
            color: white;
            border: none;
            padding: 10px 20px;
            border-radius: 5px;
            cursor: pointer;
            font-size: 14px;
            transition: all 0.3s;
        }
        .btn:hover { background: #5568d3; transform: translateY(-2px); }
        .search-box {
            width: 100%;
            padding: 12px;
            border: 2px solid #e9ecef;
            border-radius: 8px;
            font-size: 14px;
            margin-bottom: 20px;
        }
        .search-box:focus { outline: none; border-color: #667eea; }
        .keyword-editor {
            background: #f8f9fa;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 20px;
        }
        .keyword-item {
            background: white;
            padding: 15px;
            border-radius: 6px;
            margin-bottom: 15px;
            border-left: 4px solid #667eea;
        }
        .keyword-item h4 { margin-bottom: 10px; color: #667eea; }
        .keyword-list { display: flex; flex-wrap: wrap; gap: 8px; }
        .keyword-tag {
            background: #e7e9fc;
            color: #667eea;
            padding: 5px 12px;
            border-radius: 15px;
            font-size: 13px;
        }
        .loading {
            text-align: center;
            padding: 40px;
            color: #999;
        }
        .paper-card {
            background: white;
            border: 1px solid #e9ecef;
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 15px;
            transition: all 0.3s;
        }
        .paper-card:hover {
            box-shadow: 0 4px 12px rgba(0,0,0,0.1);
            transform: translateY(-2px);
        }
        .paper-title {
            font-size: 1.2em;
            color: #667eea;
            margin-bottom: 10px;
            font-weight: 600;
        }
        .paper-meta {
            color: #666;
            font-size: 0.9em;
            margin-bottom: 10px;
        }
        .paper-abstract {
            color: #333;
            line-height: 1.6;
            margin-top: 10px;
        }
        .pagination {
            display: flex;
            justify-content: center;
            gap: 10px;
            margin-top: 30px;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📚 AutoReadPaper 管理面板</h1>
            <p>论文搜索、AI总结、向量存储管理系统</p>
        </div>
        
        <div class="tabs">
            <div class="tab active" onclick="switchTab('stats')">📊 统计概览</div>
            <div class="tab" onclick="switchTab('papers')">📄 论文库</div>
            <div class="tab" onclick="switchTab('keywords')">🔑 关键词配置</div>
        </div>
        
        <div class="content">
            <!-- 统计概览 -->
            <div id="stats-tab" class="tab-content active">
                <div class="stats-grid" id="stats-grid">
                    <div class="loading">加载中...</div>
                </div>
            </div>
            
            <!-- 论文库 -->
            <div id="papers-tab" class="tab-content">
                <input type="text" class="search-box" id="paper-search" placeholder="🔍 搜索论文标题或作者...">
                <div style="margin-bottom: 20px;">
                    <button class="btn" onclick="loadPapers()">🔄 刷新</button>
                    <button class="btn" onclick="loadPapers('arxiv')">arXiv</button>
                    <button class="btn" onclick="loadPapers('pubmed')">PubMed</button>
                    <button class="btn" onclick="loadPapers()">全部</button>
                </div>
                <div id="papers-list">
                    <div class="loading">加载中...</div>
                </div>
                <div class="pagination" id="pagination"></div>
            </div>
            
            <!-- 关键词配置 -->
            <div id="keywords-tab" class="tab-content">
                <button class="btn" onclick="reloadKeywords()">🔄 重新加载配置</button>
                <div id="keywords-editor" class="keyword-editor">
                    <div class="loading">加载中...</div>
                </div>
            </div>
        </div>
    </div>

    <script>
        let currentPage = 0;
        const pageSize = 20;
        
        // 切换标签页
        function switchTab(tabName) {
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            event.target.classList.add('active');
            document.getElementById(tabName + '-tab').classList.add('active');
            
            if (tabName === 'stats') loadStats();
            if (tabName === 'papers') loadPapers();
            if (tabName === 'keywords') loadKeywords();
        }
        
        // 加载统计信息
        async function loadStats() {
            try {
                const response = await fetch('/api/stats');
                const data = await response.json();
                
                document.getElementById('stats-grid').innerHTML = `
                    <div class="stat-card">
                        <h3>${data.total_papers}</h3>
                        <p>📄 论文总数</p>
                    </div>
                    <div class="stat-card">
                        <h3>${data.collection}</h3>
                        <p>📁 向量集合</p>
                    </div>
                    <div class="stat-card">
                        <h3>${data.embedding_model.split('/').pop()}</h3>
                        <p>🧠 Embedding 模型</p>
                    </div>
                `;
            } catch (error) {
                document.getElementById('stats-grid').innerHTML = '<div class="loading">❌ 加载失败</div>';
            }
        }
        
        // 加载论文列表
        async function loadPapers(source = null) {
            const listDiv = document.getElementById('papers-list');
            listDiv.innerHTML = '<div class="loading">加载中...</div>';
            
            try {
                const url = `/api/papers/list?limit=${pageSize}&offset=${currentPage * pageSize}` + 
                           (source ? `&source=${source}` : '');
                const response = await fetch(url);
                const data = await response.json();
                
                if (data.papers.length === 0) {
                    listDiv.innerHTML = '<div class="loading">暂无论文数据</div>';
                    return;
                }
                
                listDiv.innerHTML = data.papers.map(paper => `
                    <div class="paper-card">
                        <div class="paper-title">
                            <a href="${paper.url}" target="_blank" style="color: #667eea; text-decoration: none;">
                                ${paper.title}
                            </a>
                        </div>
                        <div class="paper-meta">
                            📰 ${paper.source} | 
                            📅 ${paper.published_date} | 
                            👤 ${paper.authors.split(',').slice(0, 3).join(', ')}
                            ${paper.categories ? ' | 🏷️ ' + paper.categories : ''}
                        </div>
                        <div class="paper-abstract">${paper.abstract_snippet}</div>
                    </div>
                `).join('');
                
                // 更新分页
                const totalPages = Math.ceil(data.total / pageSize);
                updatePagination(totalPages);
            } catch (error) {
                listDiv.innerHTML = '<div class="loading">❌ 加载失败: ' + error.message + '</div>';
            }
        }
        
        function updatePagination(totalPages) {
            const paginationDiv = document.getElementById('pagination');
            if (totalPages <= 1) {
                paginationDiv.innerHTML = '';
                return;
            }
            
            let html = '';
            if (currentPage > 0) {
                html += `<button class="btn" onclick="changePage(${currentPage - 1})">◀ 上一页</button>`;
            }
            html += `<span style="padding: 10px;">第 ${currentPage + 1} / ${totalPages} 页</span>`;
            if (currentPage < totalPages - 1) {
                html += `<button class="btn" onclick="changePage(${currentPage + 1})">下一页 ▶</button>`;
            }
            paginationDiv.innerHTML = html;
        }
        
        function changePage(page) {
            currentPage = page;
            loadPapers();
        }
        
        // 加载关键词配置
        async function loadKeywords() {
            const editorDiv = document.getElementById('keywords-editor');
            editorDiv.innerHTML = '<div class="loading">加载中...</div>';
            
            try {
                const response = await fetch('/api/keywords');
                const data = await response.json();
                
                if (!data.research_areas || data.research_areas.length === 0) {
                    editorDiv.innerHTML = '<div class="loading">暂无关键词配置</div>';
                    return;
                }
                
                editorDiv.innerHTML = data.research_areas.map(area => `
                    <div class="keyword-item">
                        <h4>
                            ${area.enabled ? '✅' : '❌'} ${area.name}
                            <span style="float: right; font-size: 0.8em; color: #999;">
                                最多 ${area.max_results} 篇
                            </span>
                        </h4>
                        <div class="keyword-list">
                            ${area.keywords.map(kw => `<span class="keyword-tag">${kw}</span>`).join('')}
                        </div>
                        <div style="margin-top: 10px; color: #666; font-size: 0.9em;">
                            来源: ${area.sources ? area.sources.join(', ') : '全部'}
                        </div>
                    </div>
                `).join('');
                
                editorDiv.innerHTML += `
                    <div style="margin-top: 20px; padding: 15px; background: #fff3cd; border-radius: 6px;">
                        💡 <strong>提示：</strong>要修改关键词，请编辑 <code>config/keywords.yaml</code> 文件，
                        然后点击"重新加载配置"按钮使更改生效（无需重启容器）。
                    </div>
                `;
            } catch (error) {
                editorDiv.innerHTML = '<div class="loading">❌ 加载失败: ' + error.message + '</div>';
            }
        }
        
        // 重新加载关键词
        async function reloadKeywords() {
            try {
                const response = await fetch('/api/keywords/reload', { method: 'POST' });
                const data = await response.json();
                alert('✅ 配置已重新加载！');
                loadKeywords();
            } catch (error) {
                alert('❌ 重新加载失败: ' + error.message);
            }
        }
        
        // 论文搜索
        document.getElementById('paper-search')?.addEventListener('input', (e) => {
            const searchTerm = e.target.value.toLowerCase();
            document.querySelectorAll('.paper-card').forEach(card => {
                const text = card.textContent.toLowerCase();
                card.style.display = text.includes(searchTerm) ? 'block' : 'none';
            });
        });
        
        // 初始加载
        loadStats();
    </script>
</body>
</html>
    """
    return html_content
