# 图像处理工作流

## 边界与链路

输入是已经授权的 `ImageProcessingJob` 与原始 image package，绝不是 PDF 或 MinerU package：

```text
authorized ImageProcessingJob + initial image package
  -> source path/hash/media/dimension verification
  -> external MolYOLO / YOLOv10 detection
  -> coordinate normalization + stable M-ID + deterministic crop
  -> one crop per Gemini OCSR request
  -> MoleculeCropSet + GeminiOCSRResult
```

没有有效、hash-bound release receipt 就不能有 image job。图像工作流不创建 job，不做 Semantic Binder 或 molecule identity binding，不解释箭头，也不构建最终 Image Package。它的输出止于 `MoleculeCropSet` 和 `GeminiOCSRResult`。

## 私有模型与证据

crop、SMILES 和 detector 细节是 image-lane 私有模型；不要为它们扩展冻结的全局 document contracts。每个源文件都会校验路径、hash、媒体类型与尺寸；坐标规范化、稳定 M-ID 和 crop 都是确定性的。MolYOLO 权重与 vendored runtime 留在 Git 外，但其 provenance 会记录。

当前接受的证据只包括 unit/fake/recorded。没有接受过真实权重/GPU run，也没有接受过 live Gemini OCSR canary；文章路由的五篇 live 验证不构成 image OCSR 验证。

## 固定 live 配置

默认离线且不应成为 CI 的 live 调用。经明确授权的 OCSR live 调用要求 HTTPS base URL，且必须以 `/v1` 结尾；与文章路由不同，它不允许 loopback HTTP。凭据仅从进程环境获取，CLI 不直接读取 `.env`。

v1 固定为：每个 crop 一个请求、四个 worker、两次重试、120 秒 timeout、`gemini-3.7-flash-high`、`temperature=0`。具体 CLI 参数与恢复规则见 [CLI guide](CLI.md)。
