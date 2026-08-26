# Data provenance

## Shared paper corpus

The two supplied packs contained byte-identical copies of five PDFs. The repository keeps one deduplicated copy in `corpus/papers/`:

| Paper ID | SHA-256 |
|---|---|
| `ineleganolide` | `3ddb93a0267dbce84dd083eb6a1c3ff063ef85f61d01681ff50d5b3de766d7b1` |
| `alchivemycin_A` | `5ad8252e2ef59f608c7e4c47720b9ef4899e672b73d374d2e73fcb11d46a236f` |
| `hyperfirin` | `98173d29f2fa21e540559562285ba382d25eb63f374985db47c88b0ebf433759` |
| `kalmanol` | `7297073433fd7008dd1a9611d566705ee322cf794cbf54d774077959263c8680` |
| `principinol_B` | `38b78065c24f052132ef2f0039f16e3a27633506e669f32640d668fc4f779fc1` |

## Kalmanol page repair

The originally supplied `hires_pages/kalmanol/page-4.png` was truncated. It failed PIL verification and Tesseract/libpng reading. It was replaced by a page-4-only render from the hash-matched Kalmanol PDF at 220 dpi using `pdftoppm`.

- Original truncated SHA-256: `3c2d386c889c31cf30d41efcc3f44168c813299de232649a2adcd4a54a73ae70`
- Distributed replacement SHA-256: `a892469066c68d0b63df2bc8a95bd58c78bb169052c9c6371d22df89c9a67f0b`
- Replacement dimensions: 1819 × 2573 RGB pixels

The machine-readable repair record is `experiments/ocr_ablation/manifests/kalmanol_page_4_repair.json`. The old source-pack hash manifest is retained for provenance and therefore describes the pre-repair package; the root unified manifest describes the actual distributed files.

## Source-pack preservation

Both supplied controller prompts are preserved verbatim under their experiment directories. Source pack READMEs, protocols/selections, source manifests, original pack hash manifests, templates, route images, previews, high-resolution pages, and the controller-only OCR reference are retained in normalized locations. Old runs, pilots, caches, OCR intermediates, and installed dependency directories were intentionally excluded.
