"""Super Tutor — 材料管理路由。

提供学习材料的上传（文本 / PDF 文件）与状态查询。
PDF 文件通过 PyMuPDF 提取文本后存储，后续可由 Orchestrator
在 PARSING 阶段读取完整文本送给 LLM 进行知识切片。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from super_tutor.core.database import Database
from super_tutor.core.limiter import limiter
from super_tutor.routes.dependencies import use_db
from super_tutor.routes.schemas import (
    APIResponse,
    MaterialStatusResponse,
    MaterialUploadRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/materials", tags=["materials"])

# ---------------------------------------------------------------------------
# PyMuPDF 可选导入 — 仅在 PDF 上传时触发
# ---------------------------------------------------------------------------
try:
    import fitz  # type: ignore[import-untyped]

    _HAS_PYMUPDF = True
except ImportError:  # pragma: no cover
    _HAS_PYMUPDF = False


# ===================================================================
# POST /upload — 文本内容上传（JSON）
# ===================================================================


@router.post("/upload", response_model=APIResponse, status_code=201)
@limiter.limit("10/minute")
async def upload_material(
    request: Request,
    req: MaterialUploadRequest,
    db: Database = Depends(use_db),
) -> APIResponse:
    """上传学习材料（文本内容，JSON 格式）。

    接收已提取的纯文本或 Markdown，存入数据库供后续解析。
    完整文本被保存（不截断），Orchestrator 在 PARSING 阶段
    会读取全文送给 Tutor 角色进行知识切片。

    如需直接上传 PDF 文件，请使用 ``POST /upload/file``。
    """
    return await _store_material(
        db=db,
        title=req.title,
        content=req.content,
        subject=req.subject,
        description=req.description,
    )


# ===================================================================
# POST /upload/file — PDF 文件上传（multipart/form-data）
# ===================================================================


@router.post("/upload/file", response_model=APIResponse, status_code=201)
@limiter.limit("5/minute")
async def upload_material_file(
    request: Request,
    file: UploadFile = File(..., description="PDF 教材文件（≤50MB）"),
    title: str = Form(..., description="材料标题"),
    subject: str = Form(default="", description="所属学科"),
    description: str = Form(default="", description="材料简介"),
    db: Database = Depends(use_db),
) -> APIResponse:
    """上传 PDF 教材文件，自动提取文本。

    使用 PyMuPDF 解析 PDF，提取全部页面文本后存入数据库。
    后续 Orchestrator 在 PARSING 阶段会读取全文进行知识切片。

    限制：
    - 仅接受 ``application/pdf`` 格式
    - 文件大小 ≤ 50MB
    - 扫描版 PDF（无文字层）的提取效果取决于 OCR 质量
    """
    # -- 格式校验 --------------------------------------------------------
    if file.content_type and file.content_type != "application/pdf":
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式 '{file.content_type}'，仅接受 PDF。",
        )

    # -- 分块读取文件内容（避免大文件占满内存）---------------------------
    try:
        chunks: list[bytes] = []
        total_read = 0
        MAX_SIZE = 50 * 1024 * 1024  # 50MB 硬上限
        CHUNK_SIZE = 1024 * 1024     # 1MB 分块

        while True:
            chunk = await file.read(CHUNK_SIZE)
            if not chunk:
                break
            total_read += len(chunk)
            if total_read > MAX_SIZE:
                raise HTTPException(
                    status_code=413,
                    detail=f"文件过大（已超过 50MB 上限）。",
                )
            chunks.append(chunk)

        pdf_bytes = b"".join(chunks)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to read uploaded file: %s", exc)
        raise HTTPException(
            status_code=500,
            detail="文件读取失败，请重试。",
        ) from exc

    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="上传的文件为空。")

    file_size_mb = len(pdf_bytes) / (1024 * 1024)

    logger.info(
        "PDF upload: name=%r size=%.1fMB title=%r",
        file.filename,
        file_size_mb,
        title,
    )

    # -- PyMuPDF 文本提取 ------------------------------------------------
    if not _HAS_PYMUPDF:
        raise HTTPException(
            status_code=500,
            detail="PDF 解析组件未安装。请运行: pip install pymupdf>=1.24.0",
        )

    try:
        extracted_text = _extract_pdf_text(pdf_bytes, filename=file.filename or "unknown")
    except Exception as exc:
        logger.exception("PDF text extraction failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"PDF 文本提取失败：{exc}",
        ) from exc

    if not extracted_text.strip():
        raise HTTPException(
            status_code=422,
            detail=(
                "PDF 未提取到任何文本。可能原因：扫描版 PDF（无文字层）、"
                "图片型 PDF、或加密文档。请尝试提供文字版 PDF。"
            ),
        )

    logger.info(
        "PDF text extracted: %d chars from %r",
        len(extracted_text),
        file.filename,
    )

    return await _store_material(
        db=db,
        title=title,
        content=extracted_text,
        subject=subject,
        description=description,
    )


# ===================================================================
# GET /{material_id}/status — 材料状态查询
# ===================================================================


@router.get("/{material_id}/status", response_model=APIResponse)
async def get_material_status(
    material_id: str,
    db: Database = Depends(use_db),
) -> APIResponse:
    """查询材料解析状态。

    返回材料基本信息及已解析的知识片段数量。
    """
    project = await db.get_material(material_id)
    if project is None:
        raise HTTPException(
            status_code=404,
            detail=f"材料不存在：{material_id}",
        )

    chunks = await db.list_chunks_by_material(material_id)

    return APIResponse(
        data=MaterialStatusResponse(
            material_id=material_id,
            title=project.get("title", ""),
            status=project.get("status", "unknown"),
            chunk_count=len(chunks),
            subject=project.get("subject", ""),
            created_at=project.get("created_at", ""),
        ).model_dump()
    )


# ===================================================================
# Internal helpers
# ===================================================================


async def _store_material(
    *,
    db: Database,
    title: str,
    content: str,
    subject: str,
    description: str,
) -> APIResponse:
    """存储材料到数据库（全文保存，不截断）。"""
    material_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()

    try:
        await db.create_material(
            {
                "material_id": material_id,
                "title": title,
                "content": content,  # 全文存储，Orchestrator 解析时从此读取
                "subject": subject,
                "description": description,
                "status": "draft",
                "created_at": now,
                "updated_at": now,
            }
        )
    except Exception as exc:
        logger.exception("Failed to create material record: %s", exc)
        raise HTTPException(
            status_code=500,
            detail="材料存储失败，请稍后重试。",
        ) from exc

    logger.info(
        "Material stored: id=%s title=%r subject=%r chars=%d",
        material_id,
        title,
        subject,
        len(content),
    )

    return APIResponse(
        data={
            "material_id": material_id,
            "title": title,
            "subject": subject,
            "description": description,
            "content_length": len(content),
            "status": "draft",
            "created_at": now,
        }
    )


def _extract_pdf_text(pdf_bytes: bytes, *, filename: str = "") -> str:
    """使用 PyMuPDF 从 PDF 字节流中提取全部文字。

    Args:
        pdf_bytes: PDF 文件的原始字节。
        filename: 文件名（仅用于日志）。

    Returns:
        提取的全量文本，页面间以换行分隔。

    Raises:
        ValueError: PDF 无法打开或已加密。
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    if doc.is_encrypted:
        doc.close()
        raise ValueError("PDF 已加密，无法提取文本。")

    if doc.page_count == 0:
        doc.close()
        raise ValueError("PDF 无页面内容。")

    pages_text: list[str] = []
    for page_num in range(doc.page_count):
        page = doc[page_num]
        text = page.get_text("text")  # 纯文本模式
        if text.strip():
            pages_text.append(text)

    doc.close()

    full_text = "\n\n".join(pages_text)

    if not full_text.strip():
        # 尝试 textblock 模式（对某些布局更友好）
        doc2 = fitz.open(stream=pdf_bytes, filetype="pdf")
        pages_text2: list[str] = []
        for page_num in range(doc2.page_count):
            page = doc2[page_num]
            text = page.get_text("blocks")
            if text:
                # blocks 返回 list[tuple]，提取其中的文本
                block_texts = [
                    block[4] if isinstance(block, (tuple, list)) and len(block) > 4 else str(block)
                    for block in text
                ]
                pages_text2.append("\n".join(str(b) for b in block_texts if str(b).strip()))
        doc2.close()
        full_text = "\n\n".join(pages_text2)

    return full_text
