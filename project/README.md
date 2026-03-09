# AutoReadPaper — 全自动论文追踪与推送系统

基于 **n8n + Paper Search MCP + DeepSeek + ChromaDB** 构建的全自动学术论文监控系统。支持多源抓取、关键词过滤、AI 智能总结、向量化存储和多渠道定时推送。

## 核心功能

| 功能模块 | 说明 |
|---|---|
| 多源抓取 | arXiv / PubMed / Semantic Scholar / bioRxiv |
| 关键词过滤 | 支持按研究方向配置多组关键词 |
| AI 总结 | DeepSeek V3/R1 生成中文核心要点摘要 |
| 向量存储 | ChromaDB 存储论文 Embedding，支持语义检索 |
| 去重机制 | 基于论文 ID 和标题相似度双重去重 |
| 多渠道推送 | Email / 企业微信 / 飞书 / Telegram / GitHub Issues |
| 定时调度 | n8n Schedule Trigger 驱动，灵活配置执行频率 |

## 系统架构

```
定时触发 (n8n Schedule Trigger)
        │
        ▼
┌─────────────────┐
│  Paper Search   │ ◄── arXiv / PubMed / Semantic Scholar / bioRxiv
│  API Server     │
└────────┬────────┘
         │  原始论文数据
         ▼
┌─────────────────┐
│  Dedup Filter   │ ◄── 基于 ChromaDB ID 去重 + 标题相似度过滤
└────────┬────────┘
         │  新论文数据
         ▼
┌─────────────────┐
│  DeepSeek AI    │ ◄── 生成中文摘要 + 核心创新点提取
│  Summarizer     │
└────────┬────────┘
         │  结构化论文 + AI 摘要
         ▼
┌─────────────────┐
│  Vector Store   │ ◄── ChromaDB 存储 Embedding，归档
│  (ChromaDB)     │
└────────┬────────┘
         │
         ▼
┌─────────────────────────────────┐
│         Multi-Channel Push      │
│  Email │ 企业微信 │ 飞书 │ TG   │
└─────────────────────────────────┘
```

## 快速启动

### 1. 克隆并配置环境变量

```bash
git clone https://github.com/your-username/AutoReadPaper.git
cd AutoReadPaper/project
cp .env.example .env
# 编辑 .env 文件，填入你的 API Key 和推送配置
nano .env
```

### 2. 配置研究关键词

编辑 `config/keywords.yaml`，按研究方向配置你的关键词：

```yaml
research_areas:
  - name: "LLM Agent"
    keywords: ["large language model agent", "LLM agent", "autonomous agent"]
    sources: ["arxiv", "semantic_scholar"]
    max_results: 10
```

### 3. 一键启动所有服务

```bash
chmod +x scripts/setup.sh
./scripts/setup.sh
docker-compose up -d
```

### 4. 访问 n8n 并导入工作流

- n8n 界面：`http://localhost:5678`
- 在 n8n 中导入 `workflows/auto_paper_workflow.json`
- 配置 DeepSeek Credential 和各推送渠道 Credential
- 激活工作流

## 目录结构

```
project/
├── docker-compose.yml      # 容器编排
├── Dockerfile              # Paper API 服务镜像
├── .env.example            # 环境变量模板
├── requirements.txt        # Python 依赖
├── config/
│   ├── config.yaml         # 主配置文件
│   └── keywords.yaml       # 研究关键词配置
├── src/
│   ├── paper_search.py     # 多源论文搜索
│   ├── ai_summarizer.py    # DeepSeek AI 总结
│   ├── vector_store.py     # ChromaDB 向量存储
│   ├── push_service.py     # 多渠道推送
│   └── dedup.py            # 去重工具
├── api/
│   └── server.py           # FastAPI HTTP 服务（供 n8n 调用）
├── workflows/
│   └── auto_paper_workflow.json  # n8n 工作流导入文件
└── scripts/
    ├── setup.sh            # 初始化安装脚本
    └── run.sh              # 手动触发一次完整流程
```

## 依赖服务

| 服务 | 端口 | 说明 |
|------|------|------|
| n8n | 5678 | 工作流自动化平台 |
| ChromaDB | 8000 | 向量数据库 |
| Paper API | 8080 | 论文搜索+总结后端 |
