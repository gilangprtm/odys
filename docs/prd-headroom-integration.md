# PRD: Headroom Integration ke Odys Agent Loop

**Status:** proposed
**Author:** Sira
**Date:** 2026-07-21

---

## 1. Overview

**Problem:**
Odys agent loop mengirim tool output mentah (log, grep results, file content) ke LLM dalam bentuk `output_text = _truncate(raw)`. Truncate hanya memotong char, tidak mengompresi secara semantik. Pada workload agent nyata, 60-80% token habis untuk tool output yang repetitif/noisy. Akibatnya:
- Biaya token tinggi (terutama di round 3-5 yang context-nya sudah menumpuk)
- Context window cepat penuh
- LLM response lambat karena harus baca noise

**Solution:**
Integrasikan `headroom-ai` library ke dalam `loop.py` sebagai **content-aware compressor** yang menggantikan `_truncate()` secara selektif. Compressor hanya aktif pada tool output yang melebihi threshold tertentu.

**Elevator Pitch:**
"Same answers, 20-95% fewer tokens."

---

## 2. Requirements

### Functional Requirements (FR)

| ID | Requirement | Priority |
|----|------------|----------|
| FR-1 | Compress tool output sebelum dikirim ke LLM via `compress()` | P0 |
| FR-2 | Compressor hanya aktif untuk tool output ≥ 500 chars (below threshold: no-op) | P0 |
| FR-3 | Tidak kompres system prompt, user message, atau LLM response | P0 |
| FR-4 | Headroom load gagal = graceful degradation (fallback ke `_truncate()`) | P0 |
| FR-5 | Tool `headroom_compress` tersedia untuk model (manual compress) | P1 |
| FR-6 | Tool `headroom_retrieve` tersedia untuk model (retrieve original) | P1 |
| FR-7 | SSE event `compression_stats` dikirim ke frontend (token saved) | P1 |

### Non-Functional Requirements (NFR)

| ID | Requirement | Target |
|----|------------|--------|
| NFR-1 | Compressor overhead < 200ms per tool output | P0 |
| NFR-2 | Additional RAM usage < 500MB (ONNX Runtime) | P0 |
| NFR-3 | Headroom import gagal → zero impact (no-op) | P0 |
| NFR-4 | Container image size increase < 200MB | P1 |

---

## 3. Current Architecture (Sebelum Headroom)

```
loop.py:stream_agent_loop()
  │
  ├── Round N: tool_blocks = _resolve_tool_blocks(response)
  │     │
  │     └── for block in tool_blocks:
  │           │
  │           ├── result = await execute_tool_block(block)
  │           │
  │           ├── output_text = _truncate(raw)  ← BARIS 1247-1251
  │           │   (hanya memotong chars, tanpa kompresi)
  │           │
  │           └── messages.append(tool_result_message)
  │
  ├── stream_llm_with_fallback(messages)  ← context sudah terisi output mentah
  │
  └── Round N+1: ...
```

### Masalah: `_truncate()` (line 1247)

```python
raw = result["output"] or ""
output_text = _truncate(raw)  # MAX_DISPLAY_CHARS = 8000 chars
```

Truncate = memotong dari belakang. Tidak ada:
- Content awareness (JSON vs code vs log)
- Relevance extraction (apa yang relevan dengan query?)
- Noise reduction (null values, repeated headers)

---

## 4. Target Architecture (Setelah Headroom)

```
loop.py:stream_agent_loop()
  │
  ├── _headroom_compress = try_import_headroom()  ← SEKALI saat module load
  │
  ├── Round N: tool_blocks = _resolve_tool_blocks(response)
  │     │
  │     └── for block in tool_blocks:
  │           │
  │           ├── result = await execute_tool_block(block)
  │           │
  │           ├── raw = extract_raw_output(result)
  │           │
  │           ├── if _headroom_compress and len(raw) >= 500:
  │           │     output_text = _headroom_compress(raw)  ← KOMPRESI
  │           │   else:
  │           │     output_text = _truncate(raw)  ← FALLBACK
  │           │
  │           └── messages.append(tool_result_message)
  │
  ├── stream_llm_with_fallback(messages)  ← context lebih ringan
  │
  └── Round N+1: ...
```

### Integrasi Point

Lokasi patch: `loop.py` **line ~1246-1251** (dalam loop `for i, block in enumerate(tool_blocks)`)

Sebelum:
```python
raw = result["output"] or ""
output_text = _truncate(raw)
```

Sesudah:
```python
raw = result["output"] or ""
if _headroom_compress and len(raw) >= _COMPRESS_THRESHOLD:
    try:
        output_text = _headroom_compress(raw)
    except Exception:
        output_text = _truncate(raw)
else:
    output_text = _truncate(raw)
```

---

## 5. Headroom Library Usage

### Mode: Inline Library (bukan proxy)

```python
# Lazy import — zero cost jika tidak terinstall
_headroom_compress = None

def _init_headroom():
    global _headroom_compress
    try:
        from headroom import compress as _compress
        def _compress_tool_output(text: str) -> str:
            """Compress a single tool output string."""
            result = _compress([{"role": "user", "content": text}])
            # compress() returns list of messages; extract the compressed content
            if isinstance(result, list) and result:
                return result[0].get("content", text)
            return text
        _headroom_compress = _compress_tool_output
        logger.info("[headroom] compressor loaded")
    except ImportError:
        logger.info("[headroom] not installed, using fallback _truncate")
    except Exception as e:
        logger.warning(f"[headroom] init failed: {e}")
```

### Config

```python
# Di constants.py atau settings
COMPRESS_THRESHOLD = 500     # chars — di bawah ini, skip compress
COMPRESS_STRATEGY = "auto"   # "auto" = Headroom ContentRouter pilih strategi
```

---

## 6. Risk Management

| ID | Risk | Impact | Probability | Mitigation |
|----|------|--------|-------------|------------|
| R-1 | ONNX Runtime OOM di VPS 2GB | High | Low | Lazy load; _truncate fallback; set `OMP_NUM_THREADS=1` |
| R-2 | Compressor overhead > 200ms | Medium | Low | Threshold gate; async executor thread |
| R-3 | Headroom pip install gagal (Rust build) | Medium | Medium | Use pre-built wheel `headroom-ai[ml]`; Dockerfile cache |
| R-4 | Compressed output kehilangan info kritis | High | Low | Headroom CCR reversible; threshold gate; monitor accuracy |
| R-5 | Container image size > 500MB | Low | Medium | Install `headroom-ai[ml]` (bukan `[all]`); no proxy/mcp extras |

---

## 7. Implementation Plan

### Fase 1: Library Install + Fallback Gate (1-2 jam)

| Task | File | Estimasi |
|------|------|----------|
| 1.1 Add `headroom-ai[ml]` ke `requirements.txt` | requirements.txt | 5m |
| 1.2 Add build stage di `Dockerfile` untuk Headroom Rust wheel | Dockerfile | 15m |
| 1.3 Add lazy import `_init_headroom()` di `loop.py` | loop.py | 10m |
| 1.4 Add threshold gate di tool output processing | loop.py:1246-1251 | 10m |
| 1.5 Add `COMPRESS_THRESHOLD` ke constants | constants.py | 5m |
| 1.6 Syntax check + commit | - | 5m |

**Deliverable:** Headroom compress aktif untuk tool output ≥500 chars. Fallback ke `_truncate()` jika gagal.

### Fase 2: MCP Tools + Stats (2-3 jam)

| Task | File | Estimasi |
|------|------|----------|
| 2.1 Register `headroom_compress` di tool_index | tool_index.py | 15m |
| 2.2 Register `headroom_retrieve` di tool_index | tool_index.py | 15m |
| 2.3 Add `compression_stats` SSE event | loop.py | 15m |
| 2.4 Add schema untuk kedua tool | prompts.py | 15m |
| 2.5 Test di VPS | - | 30m |

**Deliverable:** Model bisa memanggil `headroom_compress` dan `headroom_retrieve` secara manual.

### Fase 3: Tuning + Monitoring (1-2 jam)

| Task | File | Estimasi |
|------|------|----------|
| 3.1 Benchmark token savings | - | 30m |
| 3.2 Tune threshold (500 vs 1000 vs 2000) | constants.py | 15m |
| 3.3 Test accuracy (GSM8K-style queries) | - | 30m |
| 3.4 Document di README | README.md | 15m |

**Deliverable:** Threshold optimal teridentifikasi. Accuracy verified.

---

## 8. Estimasi Total

| Fase | Durasi | T-Shirt |
|------|--------|---------|
| Fase 1 (Install + Gate) | 1-2 jam | XS |
| Fase 2 (MCP Tools) | 2-3 jam | S |
| Fase 3 (Tuning) | 1-2 jam | XS |
| **Total** | **4-7 jam** | **S** |

---

## 9. Out of Scope

- ❌ Headroom Proxy mode (sidecar container) — overkill untuk satu backend
- ❌ Output token reduction (verbosity steering) — Phase 2
- ❌ Cross-agent memory — tidak relevan (Odys sudah punya Neuron)
- ❌ `headroom learn` failure mining — Phase 3
- ❌ Headroom MCP server standalone — tidak diperlukan

---

## 10. Success Metrics

| Metric | Baseline | Target |
|--------|----------|--------|
| Avg token per agent session | ~45k | ≤25k (-45%) |
| Avg LLM latency per round | ~3s | ≤2s (-33%) |
| Avg cost per session | $0.12 | ≤$0.07 (-42%) |
| Accuracy on test queries | 100% | ≥97% |
| Memory overhead | 0 | ≤500MB |

---

## 11. ADR: Kenapa Library Mode, Bukan Proxy

**Context:**
Headroom bisa dijalankan sebagai proxy server atau sebagai Python library.

**Decision:**
Gunakan **library mode** (`from headroom import compress`).

**Alternatives:**
1. **Proxy mode** (`headroom proxy --port 8787`) — menambah container, perlu rewrite `base_url` di semua provider config. Overhead network hop. Tidak worth untuk satu backend.
2. **MCP server** (`headroom mcp serve`) — lebih cocok untuk multi-agent setup. Odys sudah punya tool execution pipeline.

**Consequences:**
- ✅ Zero infra overhead (inline di dalam proses yang sama)
- ✅ Granular control (hanya compress tool output, bukan seluruh request)
- ✅ Graceful degradation (import gagal = no-op)
- ❌ Compressor berjalan di same process (bisa compete untuk RAM/CPU)
- ❌ Tidak bisa share compressor ke service lain

---

## 12. Deployment Checklist

```
[ ] requirements.txt: tambah headroom-ai[ml]>=0.32.0
[ ] Dockerfile: tambah COPY + pip install headroom-ai[ml] di layer yang tepat
[ ] loop.py: tambah _init_headroom() + threshold gate
[ ] constants.py: tambah COMPRESS_THRESHOLD = 500
[ ] VPS: rebuild image, verify headroom doctor
[ ] Test: jalankan agent mode dengan query yang memicu tool output panjang
[ ] Monitor: cek RAM usage, token count, accuracy
```
