"""
vector_store.py — 向量存储模块

基于 ChromaDB + sentence-transformers 实现:
  - 论文 Embedding 生成与持久化存储
  - 基于余弦相似度的语义去重
  - 语义相似论文检索
"""

from __future__ import annotations

import os
from dataclasses import asdict
from typing import Optional

import chromadb
from chromadb.config import Settings
from loguru import logger
from sentence_transformers import SentenceTransformer

from .paper_search import Paper


class VectorStore:
    """
    ChromaDB 向量存储，负责论文的 Embedding 入库、去重检测和语义检索。
    """

    def __init__(self, config: dict):
        vs_cfg = config.get("vector_store", {})
        self.collection_name: str = vs_cfg.get("collection_name", "papers")
        self.model_name: str = vs_cfg.get("embedding_model", "paraphrase-multilingual-MiniLM-L12-v2")
        self.dedup_threshold: float = vs_cfg.get("dedup_similarity_threshold", 0.94)
        self.search_top_k: int = vs_cfg.get("search_top_k", 10)

        chroma_host = os.getenv("CHROMA_HOST", "localhost")
        chroma_port = int(os.getenv("CHROMA_PORT", "8000"))
        chroma_token = os.getenv("CHROMA_AUTH_TOKEN", "")

        # 初始化 ChromaDB HTTP 客户端（连接独立部署的 ChromaDB 服务）
        self._client = chromadb.HttpClient(
            host=chroma_host,
            port=chroma_port,
            settings=Settings(
                chroma_client_auth_provider="chromadb.auth.token_authn.TokenAuthClientProvider",
                chroma_client_auth_credentials=chroma_token,
            ) if chroma_token else Settings(),
        )

        # 获取或创建 Collection（启用余弦距离）
        self._collection = self._client.get_or_create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": "cosine"},
        )

        # 延迟加载 Embedding 模型（首次 encode 时自动下载）
        self._embedder: Optional[SentenceTransformer] = None
        logger.info(
            f"VectorStore initialized: collection='{self.collection_name}', "
            f"model='{self.model_name}', host={chroma_host}:{chroma_port}"
        )

    def _get_embedder(self) -> SentenceTransformer:
        if self._embedder is None:
            logger.info(f"Loading embedding model: {self.model_name}")
            self._embedder = SentenceTransformer(self.model_name)
        return self._embedder

    def _embed_paper(self, paper: Paper) -> list[float]:
        """将论文标题+摘要拼接后生成 Embedding 向量。"""
        text = f"{paper.title}. {paper.abstract[:1000]}"
        embedder = self._get_embedder()
        return embedder.encode(text, normalize_embeddings=True).tolist()

    def _paper_to_metadata(self, paper: Paper) -> dict:
        """将 Paper 对象转为 ChromaDB metadata（仅保留可序列化的基础字段）。"""
        return {
            "title": paper.title[:500],                                   # ChromaDB 字段长度限制
            "source": paper.source,
            "published_date": paper.published_date,
            "url": paper.url,
            "authors": ", ".join(paper.authors[:5]),                      # 最多保留 5 位作者
            "doi": paper.doi or "",
            "citation_count": paper.citation_count or 0,
            "categories": ", ".join(paper.categories[:10]),
        }

    # ─────────────────────────────────────────────────────
    # 核心方法: 去重 + 入库
    # ─────────────────────────────────────────────────────

    def filter_new_papers(self, papers: list[Paper]) -> list[Paper]:
        """
        过滤出尚未入库的新论文（语义去重）。

        去重逻辑:
          1. 精确去重：检查 paper_id 是否已存在于 Collection。
          2. 语义去重：对精确去重后的论文进行 Embedding 查询，
             若与已有论文的余弦相似度 >= dedup_threshold，则视为重复。

        Returns:
            过滤后的新论文列表
        """
        if not papers:
            return []

        # Step 1: 精确 ID 去重
        existing_ids = self._get_existing_ids([p.paper_id for p in papers])
        id_new_papers = [p for p in papers if p.paper_id not in existing_ids]

        if not id_new_papers:
            logger.info("All papers already exist in vector store (ID match).")
            return []

        logger.info(
            f"After ID dedup: {len(id_new_papers)}/{len(papers)} papers are new."
        )

        # Step 2: 语义相似度去重（仅在 Collection 非空时执行）
        collection_count = self._collection.count()
        if collection_count == 0:
            return id_new_papers

        truly_new: list[Paper] = []
        for paper in id_new_papers:
            embedding = self._embed_paper(paper)
            results = self._collection.query(
                query_embeddings=[embedding],
                n_results=1,
                include=["distances"],
            )
            distances = results.get("distances", [[]])[0]
            if distances:
                # ChromaDB 余弦空间中 distance = 1 - similarity
                similarity = 1.0 - distances[0]
                if similarity >= self.dedup_threshold:
                    logger.debug(
                        f"Semantic duplicate (similarity={similarity:.3f}): '{paper.title[:60]}'"
                    )
                    continue
            truly_new.append(paper)

        logger.info(
            f"After semantic dedup: {len(truly_new)}/{len(id_new_papers)} papers passed."
        )
        return truly_new

    def add_papers(self, papers: list[Paper]) -> int:
        """
        将新论文批量写入 ChromaDB。

        Returns:
            成功写入的论文数量
        """
        if not papers:
            return 0

        ids, embeddings, metadatas, documents = [], [], [], []
        for paper in papers:
            try:
                emb = self._embed_paper(paper)
            except Exception as e:
                logger.error(f"Embedding failed for '{paper.title[:60]}': {e}")
                continue

            ids.append(paper.paper_id)
            embeddings.append(emb)
            metadatas.append(self._paper_to_metadata(paper))
            # document 存储完整摘要文本（用于后续全文检索）
            documents.append(f"{paper.title}\n\n{paper.abstract}")

        if not ids:
            return 0

        self._collection.upsert(
            ids=ids,
            embeddings=embeddings,
            metadatas=metadatas,
            documents=documents,
        )
        logger.info(f"Saved {len(ids)} papers to ChromaDB collection '{self.collection_name}'.")
        return len(ids)

    # ─────────────────────────────────────────────────────
    # 语义检索
    # ─────────────────────────────────────────────────────

    def semantic_search(self, query: str, top_k: int | None = None) -> list[dict]:
        """
        根据自然语言查询语义搜索论文。

        Returns:
            论文元数据字典列表（包含相似度分数）
        """
        n = top_k or self.search_top_k
        embedder = self._get_embedder()
        query_emb = embedder.encode(query, normalize_embeddings=True).tolist()

        results = self._collection.query(
            query_embeddings=[query_emb],
            n_results=min(n, self._collection.count() or 1),
            include=["metadatas", "documents", "distances"],
        )

        output = []
        for meta, doc, dist in zip(
            results["metadatas"][0],
            results["documents"][0],
            results["distances"][0],
        ):
            output.append(
                {
                    **meta,
                    "abstract_snippet": doc[:300],
                    "similarity_score": round(1.0 - dist, 4),
                }
            )
        return output

    def get_stats(self) -> dict:
        """返回 Collection 统计信息。"""
        return {
            "collection": self.collection_name,
            "total_papers": self._collection.count(),
            "embedding_model": self.model_name,
        }

    # ─────────────────────────────────────────────────────
    # 内部工具
    # ─────────────────────────────────────────────────────

    def _get_existing_ids(self, ids: list[str]) -> set[str]:
        """批量查询哪些 ID 已存在于 Collection。"""
        if not ids:
            return set()
        try:
            result = self._collection.get(ids=ids, include=[])
            return set(result.get("ids", []))
        except Exception:
            return set()
