# Car Helper Web

Car Helper 的唯一 Web 前端，使用 React、TypeScript、Vite 和 Tailwind CSS。它支持同一会话内的多轮对话、SSE 流式回答、完整历史会话列表，以及本地长期记忆的查看、修改和逐条删除。

## 本地开发

先启动后端，再启动 Vite：

```powershell
E:\Anaconda_envs\envs\langchain_v1\python.exe -m src.api
cd frontend
npm install
npm run dev
```

访问 `http://localhost:3000`。3000 是前端开发服务器，负责热更新；它会把 `/api` 请求代理到本机 7860 端口。

## 生产构建

```powershell
cd frontend
npm run build
```

构建产物写入 `frontend/dist/`。FastAPI 检测到该目录后会在 `http://127.0.0.1:7860` 托管前端。生产模式无需再启动 3000 端口；前后端保持代码和构建职责分离，但部署为同一个本地服务。

---

## 修订说明（2026-09-23 ～ 2026-09-26）

本文档描述的前后端分工与端口约定不变（3000 开发 / 7860 生产，`/api` 代理到 7860）。以下为完善后的补充，**原文一律保留不改**；详见根目录 [`CHANGELOG.md`](../CHANGELOG.md)。

1. **历史会话可删除**：History 列表每行末尾新增 `×`，点击弹出确认框；确认后一次清掉该会话的三层记录（SQLite 列表项、PostgreSQL checkpoint 正文、浏览器缓存）。确认框组件为 `src/components/ConfirmDialog.tsx`。
2. **记忆面板计数**：改为挂载即拉取、对话结束后自动刷新。
