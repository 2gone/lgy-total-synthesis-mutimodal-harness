# 文章路由工作流

## 边界与链路

输入是自包含的规范化 TS-DAF paper package，绝不是 PDF 或原始 MinerU ZIP：

```text
normalized paper package
  -> validation + canonicalization
  -> immutable canonical artifacts + concrete local runner (attempt/cache/seal/manifest/event; SQLite-derived index)
  -> recorded/offline provider or explicitly authorized live provider
  -> text-first MainArticleRouter
  -> flagged-only visual triage
  -> two immutable PROPOSED handoffs: VisualRoutingHandoff + CoarseTextHandoff
```

validation/canonicalization 后，具体 local runner 写入可恢复的 attempt、cache、seal、manifest 和 event 证据，并以 SQLite-derived index 提供可重建的本地投影；recorded/offline provider 或经明确授权的 live provider 在该执行边界内被调用。`MainArticleRouter` 只处理文本，绝不接收 image bytes。仅显式标记的资产可进入隔离的 visual triage。路由 CLI 在两个 `PROPOSED` handoff 后停止：它不签发 release receipt，也不创建 image job；后续授权必须以 handoff content hash 严格绑定 release receipt 和 image job。

## 合同、证据与路由组

两个 handoff 都是不可变、内容寻址的证据。route group ID 是 provider 可见的 `RG001`–`RG999`；Host 同时执行连续性和引用校验。Host 校验与 provider-visible schema 必须一致，不能静默修补无效模型结果。

原始 PDF/资产保持不可变；canonicalized package 是带自身 hash 的派生物。seal、event、manifest 与对象存储是真相，SQLite 投影可以由验证后的不可变证据重建。

## 已验证状态

已有五个固定 fixture 与冻结 human gold。五篇论文首次本地、显式 live 验证通过：`1 passed in 260.30s`；37 个 visual decision 全部匹配 gold，零 escalation，九个原始资产的精确分布为 `3/2/1/2/1`。receipt artifact SHA-256 是 `05c3b902035008218c1f1fec722a7db9e4dc5df801b1facc303a082af42a5019`。

较早的第二远端 route 已关闭，不能再描述为当前 blocker。

## 运行与安全

默认离线；只有明确授权后才可以使用 `route run ... --live` 或 `route resume ... --live`。路由 live base URL 必须是绝对 URL、恰好以 `/v1` 结尾、且没有 userinfo/query/fragment：允许 HTTPS；仅字面量 loopback host `127.0.0.1`、`localhost` 或 `::1` 可使用 HTTP。CLI 不直接读取 `.env`，凭据仅来自进程环境。命令与恢复细节见 [CLI guide](CLI.md)，已完成 live 证据的历史记录见 [LIVE_GOLD.md](LIVE_GOLD.md)。
