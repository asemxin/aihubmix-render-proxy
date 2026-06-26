# AIHubMix Render Proxy

一个用于 Render 的极小 OpenAI-compatible 反向代理。客户端访问 Render，Render 再访问 AIHubMix。

## 为什么不用 Hajimi 直接改

Hajimi 是 Gemini API Proxy：它把 OpenAI 格式请求转换成 Gemini 请求，并轮询 `GEMINI_API_KEYS`。它适合代理 Gemini，不是通用的 OpenAI-compatible 上游反代。这个项目只借鉴它的 Render/Docker 部署方式。

## Render 部署

1. 把本目录推到你的 GitHub 仓库。
2. Render 新建 Web Service，选择该仓库，运行环境选择 Docker。
3. 环境变量至少设置：

```env
PORT=7860
UPSTREAM_BASE_URL=https://aihubmix.com
```

`UPSTREAM_BASE_URL` 只填域名，不要带 `/v1`。客户端的 Base URL 才需要带 `/v1`。

推荐服务端保存 AIHubMix Key：

```env
UPSTREAM_API_KEY=sk-your-aihubmix-key
PROXY_API_KEY=replace-with-a-random-client-password
```

如果不设置 `UPSTREAM_API_KEY`，代理会透传客户端的 `Authorization`，客户端继续填写 AIHubMix Key。

不建议裸跑公开代理。最稳的配置是同时设置 `UPSTREAM_API_KEY` 和 `PROXY_API_KEY`。

## 客户端配置

假设 Render 地址是：

```text
https://your-service.onrender.com
```

OpenAI-compatible Base URL 填：

```text
https://your-service.onrender.com/v1
```

API Key：

- 设置了 `UPSTREAM_API_KEY`：填 `PROXY_API_KEY`
- 没设置 `UPSTREAM_API_KEY`：填你的 AIHubMix API Key

## 本地测试

```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 7860
```

非流式测试：

```bash
curl http://localhost:7860/v1/chat/completions \
  -H "Authorization: Bearer $PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"Say hello in Chinese"}]}'
```
